"""主窗口：菜单、工具栏、左缩略图 + 右页面区、状态栏。"""

from __future__ import annotations

import os
import sys
import time

from PySide6.QtCore import (
    QEvent,
    QPoint,
    QRect,
    QSettings,
    QSize,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QDesktopServices,
    QIcon,
    QKeySequence,
    QShortcut,
    QUndoStack,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDockWidget,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSizePolicy,
    QSplitter,
    QTabBar,
    QTabWidget,
    QStackedWidget,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core.annotation import AnnotationStore, AnnotationStyle
from core.exporter import ExportManager, build_plan
from core.extractor import ExtractManager, build_extract_plan
from core.history import ModifyAnnotationCommand
from core.page_number import PageNumberSpec
from core.pdf_engine import PdfDocument, PdfEngineError
from core import qc
from core import toc as tocmodel
from core.project_manager import APP_VERSION, ProjectManager
from core.region import Region
from core.render_manager import RenderManager
from core.sequence import SequenceManager
from core.workbench import BoardStore
from database.repository import ProjectError
from ui import theme
from ui import shortcuts as shortcutmap
from ui.icons import glyph_icon
from ui.number_insert import InsertNumberDialog
from ui.number_settings import NumberSettingsDialog
from ui.export_dialog import ExportDialog
from ui.backup_dialog import BackupDialog
from ui.export_tag_dialog import ExportTagDialog, safe_filename
from ui.extract_dialog import ExtractDialog
from ui.match_dialog import MatchDialog
from ui.magnifier import MagnifierController, MagnifierWindow
from ui.material_dialog import MaterialDialog
from ui.page_number_dialog import PageNumberDialog
from ui.empty_state import EmptyState
from ui.pdf_viewer import RADIUS_PRESETS, PdfViewer, ToolMode
from ui.pipe_tree import PipeTreePanel
from ui.toc_panel import TocPanel
from ui.toc_merge_dialog import TocMergeDialog, build_merge_rows
from ui.scan_dialog import ScanDialog
from ui.property_panel import PropertyPanel
from ui.settings_dialog import SettingsDialog, default_backup_dir, default_export_dir
from ui.session import DocumentSession
from ui.status_bar import StatusBar
from ui.thumbnail_panel import ThumbnailPanel
from ui.workbench import WorkbenchPanel

MAX_RECENT = 8


class _DocTabBar(QTabBar):
    """顶部文档标签条：滚轮横向滚动，标签多了不至于只剩两个箭头。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        delta = event.angleDelta().y() or event.angleDelta().x()
        if not delta:
            return
        count = self.count()
        if count <= 1:
            return
        step = -1 if delta > 0 else 1
        self.setCurrentIndex((self.currentIndex() + step) % count)
        event.accept()


class _PagePill(QLabel):
    """工具栏中间的「当前页 / 总页数」。双击可以直接输页码跳过去。"""

    double_clicked = Signal()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt 的命名
        if event.button() == Qt.MouseButton.LeftButton:
            self.double_clicked.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        # 测试里可能直接 new MainWindow 而不走 main()，这里再兜一次底
        theme.ensure_applied()
        self.setWindowTitle("QPDF — 雀雀 PDF")
        self.resize(1280, 860)
        # 小屏（1366×768 的笔记本）上别把窗口越拖越出界：给个下限，别卡太大
        self.setMinimumSize(1024, 640)
        self.setAcceptDrops(True)

        self._doc: PdfDocument | None = None
        self._settings = QSettings("QPDF", "QPDF")
        # 主题要在建控件之前定下来：QSS 是建控件那一刻套上去的，晚了会闪一下
        theme.set_theme(self._settings.value("theme", "light", type=str))
        theme.set_accent(self._settings.value("accent", "blue", type=str))
        # 框选模式这一次是为了框哪一块：scan=识别区域 annot=标注位置
        self._region_target = "scan"
        self.scan_region = None
        self.annot_region = None
        self.material_region = None
        self.page_number_region = None
        self.page_numbers = None
        self._last_project_mismatch: dict = {}
        self._scan_dialog = None
        self._match_dialog = None
        self._material_dialog = None
        self._page_dialog = None
        # 这两个对话框各自绑着一份 PDF + 一个项目文件。记下它们属于哪个会话：
        # 切标签/关标签时才知道要不要先把它们收掉，否则扫描结果会写进别的项目。
        self._scan_dialog_owner = None
        self._match_dialog_owner = None
        self._material_dialog_owner = None
        self._page_dialog_owner = None
        self._pending_subset = None
        self._extract_open_folder = False
        # 多标签页：每个标签一个完整的「PDF + 项目」会话
        self._sessions: list[DocumentSession] = []
        self._active: DocumentSession | None = None

        self.render_manager = RenderManager(self)
        self.store = AnnotationStore(self)
        self.undo_stack = QUndoStack(self)
        self.sequence = SequenceManager(self.store, self)
        self.boards = BoardStore(self)
        self.project = ProjectManager(self.store, self.sequence, self)
        self.project.bind_boards(self.boards)
        # 项目库位置从设置里来：新开的标签页都照这个目录建/找项目
        self.project.set_project_dir(self._settings.value("project_dir", "", type=str))
        self.export_manager = ExportManager(self)
        self.extract_manager = ExtractManager(self)
        self.viewer = PdfViewer(
            self.render_manager, self.store, self.undo_stack, self.sequence, self
        )
        self.viewer.setObjectName("pdfViewer")
        self.workbench = WorkbenchPanel(self)
        self.empty = EmptyState(self)
        self._saved_flash = False
        self._stack = QStackedWidget(self)
        self._stack.addWidget(self.viewer)
        self._stack.addWidget(self.workbench)
        self._stack.addWidget(self.empty)
        self._stack.currentChanged.connect(lambda _index: self._sync_mode_ui())
        self.empty.open_requested.connect(self.open_dialog)
        self.empty.path_requested.connect(self.open_pdf)
        self.thumbs = ThumbnailPanel(self)
        self.thumbs.set_render_manager(self.render_manager)
        self.pipe_tree = PipeTreePanel(self)
        self.toc = TocPanel(self)
        self._toc_nodes: tuple = ()
        # 左侧一栏三页：页面缩略图 / PDF 自带目录 / 管线号目录（F01）
        self._left_tabs = QTabWidget(self)
        self._left_tabs.setObjectName("leftTabs")
        self._left_tabs.setDocumentMode(True)
        self._left_tabs.addTab(self.thumbs, "页面")
        self._left_tabs.addTab(self.toc, "目录")
        self._left_tabs.addTab(self.pipe_tree, "目录树")
        self._left_tabs.setMinimumWidth(268)
        # 放大窗是**多个**的，集合存在程序设置里（所有项目共用一套窗口）
        self.magnifiers = MagnifierController(self, self._settings)
        self.magnifiers.pick_region_requested.connect(self._pick_magnifier_region)
        self.magnifiers.new_window_requested.connect(self._new_magnifier)
        self.magnifiers.close_all_requested.connect(self._close_all_magnifiers)
        self.magnifiers.closed.connect(self._on_magnifier_closed)
        self.magnifiers.changed.connect(self._on_magnifier_state_changed)
        self.magnifiers.page_step_requested.connect(self._step_page_from_magnifier)
        self.magnifiers.load()
        self._magnifier_pick_id = ""
        self.status = StatusBar(self)
        self.setStatusBar(self.status)
        self.props = PropertyPanel(self)

        self._splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self._splitter.addWidget(self._left_tabs)
        self._splitter.addWidget(self._stack)
        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setSizes([300, 980])

        # 顶部标签条：一个标签 = 一份 PDF + 它的项目会话（浏览器式）
        self._tabbar = _DocTabBar(self)
        self._tabbar.setObjectName("docTabs")
        self._tabbar.setMovable(True)
        self._tabbar.setExpanding(False)
        self._tabbar.setUsesScrollButtons(True)  # 标签多了横向滚动
        self._tabbar.setTabsClosable(True)
        self._tabbar.setElideMode(Qt.TextElideMode.ElideRight)
        self._tabbar.setDocumentMode(True)
        self._tabbar.setMinimumWidth(160)
        self._tabbar.setVisible(False)
        self._new_tab = QToolButton(self)
        self._new_tab.setObjectName("newTabButton")
        self._new_tab.setText("+")
        self._new_tab.setToolTip("新建标签页（打开另一个 PDF / 项目）")
        self._new_tab.setAutoRaise(True)

        self._tab_row = QWidget(self)
        row = QHBoxLayout(self._tab_row)
        row.setContentsMargins(6, 4, 6, 0)
        row.setSpacing(2)
        row.addWidget(self._tabbar, 1)
        row.addWidget(self._new_tab, 0)

        self._center = QWidget(self)
        center = QVBoxLayout(self._center)
        center.setContentsMargins(0, 0, 0, 0)
        center.setSpacing(0)
        center.addWidget(self._tab_row, 0)
        center.addWidget(self._splitter, 1)
        self.setCentralWidget(self._center)

        self.props_dock = QDockWidget("属性", self)
        self.props_dock.setObjectName("properties_dock")
        self.props_dock.setAllowedAreas(
            Qt.DockWidgetArea.RightDockWidgetArea | Qt.DockWidgetArea.LeftDockWidgetArea
        )
        self.props_dock.setWidget(self.props)
        self.props_dock.setMinimumWidth(248)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.props_dock)

        self._build_actions()
        self._build_menus()
        self._build_toolbar()
        self._connect()
        self._bind_letter_keys()
        self._setup_shortcuts()

        self._cache_timer = QTimer(self)
        self._cache_timer.setInterval(1000)
        self._cache_timer.timeout.connect(self._refresh_cache_stat)
        self._cache_timer.start()

        self._update_ui_state()

    # ------------------------------------------------------------------ 构建

    _GLYPH_ACTIONS = (
        ("act_open", "open"),
        ("act_save", "save"),
        ("act_export", "export"),
        ("act_zoom_in", "zoom_in"),
        ("act_zoom_out", "zoom_out"),
        ("act_fit_page", "fit_page"),
        ("act_fit_width", "fit_width"),
        ("act_prev", "prev"),
        ("act_next", "next"),
        ("act_undo_tb", "undo"),
        ("act_redo_tb", "redo"),
        ("act_magnifier", "lens_window"),
        ("act_material_scan", "table"),
        ("act_match_data", "link"),
    )

    def _glyph(self, name: str, color=None):
        # 颜色默认取当前主题的文字色：不写死，换深色主题时重画一遍即可（_refresh_glyphs）
        return glyph_icon(name, color, 18)

    def _refresh_glyphs(self) -> None:
        """主题变了以后把带图标的 action 重画一遍（QIcon 是位图，不会自己跟着变）。"""
        for attr, glyph in self._GLYPH_ACTIONS:
            act = getattr(self, attr, None)
            if act is not None:
                act.setIcon(self._glyph(glyph))

    def _build_actions(self) -> None:
        self.act_open = QAction(self._glyph("open"), "打开 PDF(&O)…", self)
        self.act_open.setShortcut(QKeySequence.StandardKey.Open)
        self.act_open.triggered.connect(self.open_dialog)

        self.act_new_tab = QAction("新建标签页(&N)", self)
        self.act_new_tab.setShortcut(QKeySequence("Ctrl+T"))
        self.act_new_tab.setToolTip("再开一个 PDF / 项目，各开各的标签（Ctrl+T）")
        self.act_new_tab.triggered.connect(self.open_dialog)

        self.act_close = QAction("关闭文档(&W)", self)
        self.act_close.setShortcut(QKeySequence("Ctrl+W"))
        self.act_close.setToolTip("关掉当前标签页（Ctrl+W）")
        self.act_close.triggered.connect(self.close_document)

        self.act_next_tab = QAction("下一个标签页", self)
        self.act_next_tab.setShortcut(QKeySequence("Ctrl+Tab"))
        self.act_next_tab.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        self.act_next_tab.triggered.connect(lambda: self._cycle_tab(1))

        self.act_prev_tab = QAction("上一个标签页", self)
        self.act_prev_tab.setShortcut(QKeySequence("Ctrl+Shift+Tab"))
        self.act_prev_tab.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        self.act_prev_tab.triggered.connect(lambda: self._cycle_tab(-1))

        self.act_save = QAction(self._glyph("save"), "保存项目(&S)", self)
        self.act_save.setShortcut(QKeySequence.StandardKey.Save)
        self.act_save.setToolTip("把标注写进 .qpdf 项目文件（Ctrl+S）。原始 PDF 不会被改动")
        self.act_save.triggered.connect(self.save_project)

        self.act_save_as = QAction("项目另存为(&A)…", self)
        self.act_save_as.setShortcut(QKeySequence("Ctrl+Shift+S"))
        self.act_save_as.triggered.connect(self.save_project_as)

        self.act_open_project = QAction("打开项目(&P)…", self)
        self.act_open_project.triggered.connect(self.open_project_dialog)

        self.act_backup = QAction("备份项目(&B)", self)
        self.act_backup.setToolTip("立刻留一份项目备份到 备份/ 目录")
        self.act_backup.triggered.connect(self.backup_project)

        self.act_backup_restore = QAction("备份与还原(&R)…", self)
        self.act_backup_restore.setToolTip("看备份列表、把项目还原到某一份备份（原始 PDF 不动）")
        self.act_backup_restore.triggered.connect(self.open_backup_dialog)

        self.act_export_tag = QAction("按编号批量导出(&G)…", self)
        self.act_export_tag.setToolTip("挑几个管线号，把它们出现的页面提成一个 PDF（标注跟着走）")
        self.act_export_tag.triggered.connect(lambda: self.export_by_pipe_tag(""))

        self.act_output_dir = QAction("打开输出目录(&U)", self)
        self.act_output_dir.triggered.connect(self.open_output_dir)

        self.act_export = QAction(self._glyph("export"), "导出 PDF(&E)…", self)
        self.act_export.setShortcut(QKeySequence("Ctrl+E"))
        self.act_export.setToolTip(
            "把标注写成标准 PDF 注释导出到 输出/（Ctrl+E）。原始 PDF 不会被改动"
        )
        self.act_extract = QAction("提取页面…", self)
        self.act_extract.setToolTip("把挑出来的页面另存成一个新 PDF，标注和书签一起带过去")
        self.act_extract.triggered.connect(self.extract_pages)
        self.act_export.triggered.connect(self.export_pdf)

        self.act_quit = QAction("退出(&Q)", self)
        self.act_quit.setShortcut(QKeySequence.StandardKey.Quit)
        self.act_quit.triggered.connect(self.close)

        self.act_zoom_in = QAction(self._glyph("zoom_in"), "放大", self)
        self.act_zoom_in.setShortcuts([QKeySequence("Ctrl+="), QKeySequence("Ctrl++")])
        self.act_zoom_in.triggered.connect(lambda: self._surface().zoom_in())

        self.act_zoom_out = QAction(self._glyph("zoom_out"), "缩小", self)
        self.act_zoom_out.setShortcut(QKeySequence("Ctrl+-"))
        self.act_zoom_out.triggered.connect(lambda: self._surface().zoom_out())

        self.act_fit_page = QAction(self._glyph("fit_page"), "适应窗口", self)
        self.act_fit_page.setShortcut(QKeySequence("Ctrl+1"))
        self.act_fit_page.triggered.connect(self._fit_view)

        self.act_fit_width = QAction(self._glyph("fit_width"), "适应宽度", self)
        self.act_fit_width.setShortcut(QKeySequence("Ctrl+2"))
        self.act_fit_width.triggered.connect(self._fit_view)

        self.act_actual = QAction("实际大小", self)
        self.act_actual.setShortcut(QKeySequence("Ctrl+0"))
        self.act_actual.triggered.connect(lambda: self._surface().reset_zoom())

        self.act_refresh = QAction("强制刷新", self)
        self.act_refresh.setShortcut(QKeySequence("Ctrl+F5"))
        self.act_refresh.setToolTip(
            "丢掉缓存、按当前倍率重新渲染这一页（画面空白或显示不对时点它）Ctrl+F5"
        )
        self.act_refresh.triggered.connect(self.force_refresh_page)

        self.act_prev = QAction(self._glyph("prev"), "上一页", self)
        self.act_prev.setShortcut(QKeySequence("PgUp"))
        self.act_prev.triggered.connect(self.viewer.prev_page)

        self.act_next = QAction(self._glyph("next"), "下一页", self)
        self.act_next.setShortcut(QKeySequence("PgDown"))
        self.act_next.triggered.connect(self.viewer.next_page)

        self.act_goto = QAction("跳转页码(&G)…", self)
        self.act_goto.setShortcut(QKeySequence("Ctrl+G"))
        self.act_goto.triggered.connect(self.goto_page_dialog)

        self.act_single = QAction("单页模式", self)
        self.act_single.setCheckable(True)
        self.act_single.toggled.connect(self.viewer.set_single_page)

        self.act_thumbs = QAction("显示缩略图", self)
        self.act_thumbs.setCheckable(True)
        self.act_thumbs.setChecked(True)
        self.act_thumbs.setShortcut(QKeySequence("Ctrl+B"))
        self.act_thumbs.toggled.connect(self.thumbs.setVisible)


        # -- 编辑（第 10 章）------------------------------------------------
        # 撤销/重做必须跟着「当前标签」的撤销栈走：多标签页下不能把动作绑死在
        # 建窗时那个栈上，所以这里是转发到 self.undo_stack 的薄动作。
        self.act_undo = QAction("撤销", self)
        self.act_undo.setShortcut(QKeySequence.StandardKey.Undo)
        self.act_undo.triggered.connect(self._undo)
        self.act_undo.setEnabled(False)

        self.act_redo = QAction("重做", self)
        self.act_redo.setShortcut(QKeySequence("Ctrl+Y"))
        self.act_redo.triggered.connect(self._redo)
        self.act_redo.setEnabled(False)

        # 工具栏用的是固定文字的副本：QUndoStack 会自动把菜单项改成
        # "撤销 修改 1 个标注的样式" 这种长文案，放在工具栏里会来回抖动。
        self.act_undo_tb = QAction(self._glyph("undo"), "撤销", self)
        self.act_undo_tb.setToolTip("撤销 (Ctrl+Z)")
        self.act_undo_tb.setEnabled(False)
        self.act_undo_tb.triggered.connect(self._undo)

        self.act_redo_tb = QAction(self._glyph("redo"), "重做", self)
        self.act_redo_tb.setToolTip("重做 (Ctrl+Y)")
        self.act_redo_tb.setEnabled(False)
        self.act_redo_tb.triggered.connect(self._redo)

        self.act_delete = QAction("删除标注", self)
        self.act_delete.setShortcut(QKeySequence("Delete"))
        self.act_delete.setEnabled(False)
        self.act_delete.triggered.connect(self._delete_selected)

        self.act_copy = QAction("复制标注", self)
        self.act_copy.setShortcut(QKeySequence("Ctrl+C"))
        self.act_copy.setEnabled(False)
        self.act_copy.triggered.connect(self._copy_selected)

        self.act_paste = QAction("粘贴标注", self)
        self.act_paste.setShortcut(QKeySequence("Ctrl+V"))
        self.act_paste.setEnabled(False)
        self.act_paste.triggered.connect(self._paste_clipboard)

        self.act_edit_text = QAction("编辑选中文字", self)
        self.act_edit_text.setShortcut(QKeySequence("Return"))
        self.act_edit_text.setEnabled(False)
        self.act_edit_text.triggered.connect(self.viewer.edit_selected)

        # -- 标注管理（第 8.3 节）--------------------------------------------
        self.act_lock = QAction("锁定 / 解锁标注", self)
        self.act_lock.setShortcut(QKeySequence("Ctrl+L"))
        self.act_lock.setEnabled(False)
        self.act_lock.triggered.connect(self._toggle_locked)

        self.act_hide = QAction("隐藏选中标注", self)
        self.act_hide.setShortcut(QKeySequence("Ctrl+H"))
        self.act_hide.setEnabled(False)
        self.act_hide.triggered.connect(self._toggle_hidden)

        self.act_show_all = QAction("显示全部标注", self)
        self.act_show_all.setCheckable(True)
        self.act_show_all.setChecked(True)
        self.act_show_all.setShortcut(QKeySequence("Ctrl+U"))
        self.act_show_all.setToolTip("临时藏起所有标注看底图（不影响标注本身）（Ctrl+U）")
        self.act_show_all.toggled.connect(self.viewer.set_annotations_visible)
        self.act_show_all.toggled.connect(self._refresh_magnifier_annotations)

        self.act_unhide_all = QAction("取消所有隐藏", self)
        self.act_unhide_all.setEnabled(False)
        self.act_unhide_all.triggered.connect(self._unhide_all)

        self.act_eye_care = QAction("护眼模式（淡绿底）", self)
        self.act_eye_care.setCheckable(True)
        self.act_eye_care.setShortcut(QKeySequence("Ctrl+Shift+E"))
        self.act_eye_care.setToolTip("把页面区底色换成淡豆绿，图纸本身不变色（Ctrl+Shift+E）")
        self.act_eye_care.toggled.connect(self._toggle_eye_care)
        # 上次关掉的话这次还是关着的
        self._apply_render_budget()
        if self._settings.value("eye_care", False, type=bool):
            self.act_eye_care.setChecked(True)
            theme.set_eye_care(True)

        # -- 工具模式（第 12.2 / 13 章）--------------------------------------
        self._mode_group = QActionGroup(self)
        self._mode_group.setExclusive(True)

        self.act_mode_browse = QAction("浏览", self)
        self.act_mode_browse.setCheckable(True)
        self.act_mode_browse.setChecked(True)
        # V 由 _bind_letter_keys() 绑在画布上（窗口级的 V 会在搜索框里抢字）
        self.act_mode_browse.setToolTip("浏览模式：按住左键拖动页面（V / Esc）")
        self.act_mode_browse.setData(ToolMode.BROWSE)

        self.act_mode_text = QAction("文字", self)
        self.act_mode_text.setCheckable(True)
        self.act_mode_text.setShortcut(QKeySequence("F5"))
        self.act_mode_text.setToolTip("文字标注模式（F5）")
        self.act_mode_text.setData(ToolMode.TEXT)

        self.act_mode_number = QAction("数字", self)
        self.act_mode_number.setCheckable(True)
        self.act_mode_number.setShortcut(QKeySequence("F2"))
        self.act_mode_number.setToolTip("连续数字标注（F2）")
        self.act_mode_number.setData(ToolMode.NUMBER)

        self.act_mode_overwrite = QAction("覆盖", self)
        self.act_mode_overwrite.setCheckable(True)
        self.act_mode_overwrite.setShortcut(QKeySequence("F3"))
        self.act_mode_overwrite.setToolTip("覆盖标注：点旧数字，把它挪到新位置（编号不变）（F3）")
        self.act_mode_overwrite.setData(ToolMode.OVERWRITE)

        self.act_mode_insert = QAction("插队", self)
        self.act_mode_insert.setCheckable(True)
        self.act_mode_insert.setShortcut(QKeySequence("F4"))
        self.act_mode_insert.setToolTip("插队标注：在已有编号中间补一个，后面的自动后移（F4）")
        self.act_mode_insert.setData(ToolMode.INSERT)

        self.act_mode_select = QAction("框选", self)
        self.act_mode_select.setCheckable(True)
        self.act_mode_select.setShortcut(QKeySequence("F6"))
        self.act_mode_select.setToolTip("框选删除：拖出矩形选中本工具的标注，Delete 删除（F6）")
        self.act_mode_select.setData(ToolMode.SELECT)

        self.act_scan_region = QAction("设置识别区域…", self)
        self.act_scan_region.setToolTip(
            "在页面上拖一个矩形，框住管线号/图号所在位置；之后批量识别都从这块地方读"
        )
        self.act_scan_region.setCheckable(True)
        self.act_scan_region.setData(ToolMode.REGION)

        self.act_annot_region = QAction("设置标注位置…", self)
        self.act_annot_region.setToolTip(
            "在页面上拖一个矩形，框住要把匹配结果写上去的位置（数据匹配标注用）"
        )
        self.act_annot_region.setCheckable(True)
        self.act_annot_region.setData(ToolMode.REGION)

        self.act_match_data = QAction("数据匹配标注…", self)
        self.act_match_data.setShortcut(QKeySequence("Ctrl+Shift+M"))
        self.act_match_data.setToolTip(
            "读一张 CSV / Excel，把每页识别到的管线号和表里的记录对上，"
            "自动把探伤比例这类内容写到图上（Ctrl+Shift+M）"
        )
        self.act_match_data.triggered.connect(self.open_match_dialog)

        self.act_scan_all = QAction("批量识别管线号…", self)
        self.act_scan_all.setShortcut(QKeySequence("Ctrl+Shift+O"))
        self.act_scan_all.setToolTip("把每一页识别区域里的字读一遍，做成书签（后台线程，界面不卡）")
        self.act_scan_all.triggered.connect(self.open_scan_dialog)

        self.act_material_region = QAction("设置材料表区域…", self)
        self.act_material_region.setToolTip(
            "在页面上拖一个矩形，框住图纸右上角的材料表；之后每页都从同样的位置读"
        )
        self.act_material_region.setCheckable(True)
        self.act_material_region.setData(ToolMode.REGION)

        self.act_material_scan = QAction("材料表识别…", self)
        self.act_material_scan.setShortcut(QKeySequence("Ctrl+Shift+D"))
        self.act_material_scan.setToolTip(
            "逐页 OCR 材料表，汇总「管线号 / 页码 / 序号 / 材质 / 材料 / 标准 / Φ规格 / DN / 数量」并导出 Excel"
        )
        self.act_material_scan.triggered.connect(self.open_material_dialog)

        self.act_page_number_region = QAction("设置页码位置…", self)
        self.act_page_number_region.setToolTip(
            "在页面上拖一个矩形，框出页码要放的位置；所有页共用这个比例位置"
        )
        self.act_page_number_region.setCheckable(True)
        self.act_page_number_region.setData(ToolMode.REGION)

        self.act_page_number = QAction("插入/设置页码…", self)
        self.act_page_number.setShortcut(QKeySequence("Ctrl+Shift+N"))
        self.act_page_number.setToolTip(
            "设置页码格式（如「第 {page} 页」）和字体/颜色；页码会写进导出的 PDF，"
            "但不会进 .qpdf 标注库，qreport 不会把它当焊口数字（Ctrl+Shift+N）"
        )
        self.act_page_number.triggered.connect(self.open_page_number_dialog)

        self.act_page_number_clear = QAction("清除页码", self)
        self.act_page_number_clear.setToolTip("不再在预览/导出里写页码")
        self.act_page_number_clear.triggered.connect(self.clear_page_numbers)

        self.act_pipe_tree = QAction("管线号目录(&P)", self)
        self.act_pipe_tree.setShortcut(QKeySequence("Ctrl+Shift+I"))
        self.act_pipe_tree.setToolTip("左侧切换到目录树（Ctrl+Shift+I）")
        self.act_pipe_tree.triggered.connect(self.show_pipe_tree)

        self.act_merge_toc = QAction("比对书签与识别结果…", self)
        self.act_merge_toc.setToolTip(
            "把 PDF 自带的书签和 OCR 识别到的管线号摆在一起核对，勾选要的写进目录树"
        )
        self.act_merge_toc.triggered.connect(self.merge_toc_with_bookmarks)

        self.act_toc = QAction("目录(&C)", self)
        self.act_toc.setShortcut(QKeySequence("Ctrl+Shift+T"))
        self.act_toc.setToolTip("左侧切换到这份 PDF 自带的目录（Ctrl+Shift+T）")
        self.act_toc.triggered.connect(self.show_toc)

        self.act_magnifier = QAction("放大窗", self)
        self.act_magnifier.setCheckable(True)
        self.act_magnifier.setShortcut(QKeySequence("Ctrl+Shift+L"))
        self.act_magnifier.setToolTip(
            "显示/隐藏放大窗（Ctrl+Shift+L）：可以开多个，右键能置顶、设置本窗区域、锁定；"
            "拖边改大小 = 看更多页面，滚轮改窗口，Ctrl+滚轮 = 缩放内容，Ctrl+拖动 = 移内容；"
            "侧键在放大窗上是上一页/下一页"
        )
        self.act_magnifier.toggled.connect(self._on_magnifier_toggled)

        self.act_magnifier_new = QAction("新建放大窗", self)
        self.act_magnifier_new.setToolTip("再开一个放大窗（复制当前窗口的区域，位置自动错开）")
        self.act_magnifier_new.triggered.connect(lambda: self._new_magnifier(None))

        self.act_magnifier_region = QAction("设置放大区域…", self)
        self.act_magnifier_region.setToolTip("框住当前放大窗要看的那块地方（每页按同样比例取）")
        self.act_magnifier_region.triggered.connect(self._pick_active_magnifier_region)

        self._mode_actions = {
            ToolMode.BROWSE: self.act_mode_browse,
            ToolMode.TEXT: self.act_mode_text,
            ToolMode.NUMBER: self.act_mode_number,
            ToolMode.OVERWRITE: self.act_mode_overwrite,
            ToolMode.INSERT: self.act_mode_insert,
            ToolMode.SELECT: self.act_mode_select,
            ToolMode.REGION: self.act_scan_region,
        }

        for act in (
            self.act_mode_browse,
            self.act_mode_text,
            self.act_mode_number,
            self.act_mode_overwrite,
            self.act_mode_insert,
            self.act_mode_select,
        ):
            act.triggered.connect(lambda _=False, a=act: self._surface().set_mode(a.data()))
            self._mode_group.addAction(act)

        # 两块区域共用「框选」模式，靠 _region_target 区分这一次框的是哪一块
        self.act_scan_region.triggered.connect(lambda: self._start_region_pick("scan"))
        self.act_annot_region.triggered.connect(lambda: self._start_region_pick("annot"))
        self.act_material_region.triggered.connect(lambda: self._start_region_pick("material"))
        self.act_page_number_region.triggered.connect(
            lambda: self._start_region_pick("page_number")
        )
        self._mode_group.addAction(self.act_scan_region)
        self._mode_group.addAction(self.act_annot_region)
        self._mode_group.addAction(self.act_material_region)
        self._mode_group.addAction(self.act_page_number_region)

        # -- 编号（第 5.4 节）------------------------------------------------
        self.act_restart_number = QAction("重新从 1 编号", self)
        self.act_restart_number.setShortcut(QKeySequence("Ctrl+Shift+R"))
        self.act_restart_number.setToolTip("新开一组编号，从 1 重新开始（Ctrl+Shift+R）")
        self.act_restart_number.triggered.connect(self.restart_numbering)

        self.act_number_settings = QAction("数字起始值与步长(&N)…", self)
        self.act_number_settings.triggered.connect(self.number_settings_dialog)

        # -- 工作台 ----------------------------------------------------------
        self.act_workbench = QAction("工作台", self)
        self.act_workbench.setCheckable(True)
        self.act_workbench.setShortcut(QKeySequence("F7"))
        self.act_workbench.setToolTip(
            "工作台：把相关几页摆在一块画布上一起看、一起标（F7，不影响 PDF 页序）"
        )
        self.act_workbench.toggled.connect(self.set_workbench_visible)

        self.act_add_to_board = QAction("把当前页加入工作台", self)
        self.act_add_to_board.setShortcut(QKeySequence("F8"))
        self.act_add_to_board.setToolTip("把正在看的这一页丢进工作台（F8）")
        self.act_add_to_board.triggered.connect(self.add_current_page_to_board)

        self.act_board_pages = QAction("添加页面到工作台(&A)…", self)
        self.act_board_pages.triggered.connect(self.workbench.add_pages_dialog)

        # -- 覆盖模式的碰撞检测半径（第 6.1.2 节）------------------------------
        self._radius_group = QActionGroup(self)
        self._radius_group.setExclusive(True)
        self._radius_actions: dict[str, QAction] = {}
        for label, radius in RADIUS_PRESETS.items():
            act = QAction(f"{label}（{radius:g} pt）", self)
            act.setCheckable(True)
            act.setChecked(radius == self.viewer.detect_radius)
            act.setData(radius)
            act.triggered.connect(lambda _=False, r=radius: self.set_detect_radius(r))
            self._radius_group.addAction(act)
            self._radius_actions[label] = act
        self.act_radius_custom = QAction("自定义…", self)
        self.act_radius_custom.triggered.connect(self.custom_detect_radius)

        self.act_props = self.props_dock.toggleViewAction()
        self.act_props.setText("显示属性面板")

        self.act_about = QAction("关于 QPDF", self)
        self.act_about.triggered.connect(self.show_about)

        self.act_settings = QAction("设置(&S)…", self)
        self.act_settings.setShortcut(QKeySequence("Ctrl+,"))
        self.act_settings.triggered.connect(self.open_settings)

        self.act_assoc = QAction("让 .qpdf 显示本软件图标…", self)
        self.act_assoc.triggered.connect(self.register_qpdf_assoc)
        self.act_unassoc = QAction("取消 .qpdf 关联", self)
        self.act_unassoc.triggered.connect(self.unregister_qpdf_assoc)
        self.act_check_project = QAction("检查项目与 PDF 是否对应…", self)
        self.act_check_project.setToolTip("确认旁边的 .qpdf 是不是属于这份 PDF")
        self.act_check_project.triggered.connect(self.check_project_source)

    def _build_menus(self) -> None:
        bar = self.menuBar()

        m_file = bar.addMenu("文件(&F)")
        m_file.addAction(self.act_open)
        m_file.addAction(self.act_open_project)
        self._recent_menu = m_file.addMenu("最近打开")
        m_file.addSeparator()
        m_file.addAction(self.act_save)
        m_file.addAction(self.act_save_as)
        m_file.addAction(self.act_backup)
        m_file.addAction(self.act_backup_restore)
        m_file.addAction(self.act_output_dir)
        m_file.addSeparator()
        m_file.addAction(self.act_export)
        m_file.addAction(self.act_extract)
        m_file.addAction(self.act_export_tag)
        m_file.addSeparator()
        m_file.addAction(self.act_new_tab)
        m_file.addAction(self.act_next_tab)
        m_file.addAction(self.act_prev_tab)
        m_file.addAction(self.act_close)
        m_file.addSeparator()
        m_file.addAction(self.act_quit)

        m_edit = bar.addMenu("编辑(&E)")
        m_edit.addAction(self.act_undo)
        m_edit.addAction(self.act_redo)
        m_edit.addSeparator()
        m_edit.addAction(self.act_edit_text)
        m_edit.addAction(self.act_copy)
        m_edit.addAction(self.act_paste)
        m_edit.addAction(self.act_delete)
        m_edit.addSeparator()
        m_edit.addAction(self.act_lock)
        m_edit.addAction(self.act_hide)
        m_edit.addAction(self.act_unhide_all)

        m_tool = bar.addMenu("工具(&T)")
        self._tools_menu = m_tool
        m_tool.addAction(self.act_mode_browse)
        m_tool.addAction(self.act_mode_text)
        m_tool.addAction(self.act_mode_number)
        m_tool.addAction(self.act_mode_overwrite)
        m_tool.addAction(self.act_mode_insert)
        m_tool.addAction(self.act_mode_select)
        m_tool.addSeparator()
        m_tool.addAction(self.act_scan_region)
        m_tool.addAction(self.act_scan_all)
        m_tool.addSeparator()
        m_tool.addAction(self.act_material_region)
        m_tool.addAction(self.act_material_scan)
        m_tool.addSeparator()
        m_tool.addAction(self.act_page_number_region)
        m_tool.addAction(self.act_page_number)
        m_tool.addAction(self.act_page_number_clear)
        m_tool.addAction(self.act_pipe_tree)
        m_tool.addAction(self.act_merge_toc)
        m_tool.addSeparator()
        m_tool.addAction(self.act_annot_region)
        m_tool.addAction(self.act_match_data)
        m_tool.addSeparator()
        m_tool.addAction(self.act_restart_number)
        m_tool.addAction(self.act_number_settings)
        m_tool.addSeparator()
        m_tool.addAction(self.act_workbench)
        m_tool.addAction(self.act_add_to_board)
        m_tool.addAction(self.act_board_pages)
        m_radius = m_tool.addMenu("覆盖检测半径")
        for act in self._radius_actions.values():
            m_radius.addAction(act)
        m_radius.addAction(self.act_radius_custom)

        m_view = bar.addMenu("视图(&V)")
        m_view.addAction(self.act_zoom_in)
        m_view.addAction(self.act_zoom_out)
        m_view.addSeparator()
        m_view.addAction(self.act_fit_page)
        m_view.addAction(self.act_fit_width)
        m_view.addAction(self.act_actual)
        m_view.addSeparator()
        m_view.addAction(self.act_refresh)
        m_view.addSeparator()
        m_view.addAction(self.act_prev)
        m_view.addAction(self.act_next)
        m_view.addAction(self.act_goto)
        m_view.addSeparator()
        m_view.addAction(self.act_toc)
        m_view.addAction(self.act_magnifier)
        m_view.addAction(self.act_magnifier_new)
        m_view.addAction(self.act_magnifier_region)
        m_view.addSeparator()
        m_view.addAction(self.act_workbench)
        m_view.addSeparator()
        m_view.addAction(self.act_single)
        m_view.addAction(self.act_thumbs)
        m_view.addAction(self.act_props)
        m_view.addSeparator()
        m_view.addAction(self.act_show_all)
        m_view.addAction(self.act_eye_care)
        m_view.addSeparator()
        m_view.addMenu(self._build_theme_menu(m_view))

        m_help = bar.addMenu("帮助(&H)")
        m_help.addAction(self.act_check_project)
        m_help.addSeparator()
        m_help.addAction(self.act_assoc)
        m_help.addAction(self.act_unassoc)
        m_help.addSeparator()
        m_help.addAction(self.act_about)

        # 工具菜单里补「设置」——它在 _build_menus 里已经建好，这里只挂动作
        if hasattr(self, "_tools_menu"):
            self._tools_menu.addSeparator()
            self._tools_menu.addAction(self.act_settings)

        self._rebuild_recent_menu()

    def _build_theme_menu(self, parent: QWidget) -> QMenu:
        """视图 → 界面外观：浅色 / 浅粉 / 深色 + 五套强调色。

        只换「取景框」的颜色。图纸永远是白纸——工程图纸上了深色底，
        线条和填充就全糊了，这是底线。
        """
        menu = QMenu("界面外观", parent)
        self._theme_actions: dict[str, QAction] = {}
        group = QActionGroup(menu)
        group.setExclusive(True)
        for key, label in theme.theme_choices():
            act = QAction(label, menu)
            act.setCheckable(True)
            act.setChecked(theme.theme_name() == key)
            act.triggered.connect(lambda _=False, k=key: self.apply_theme(name=k))
            menu.addAction(act)
            group.addAction(act)
            self._theme_actions[key] = act

        menu.addSeparator()
        accents = menu.addMenu("强调色")
        self._accent_actions: dict[str, QAction] = {}
        group = QActionGroup(accents)
        group.setExclusive(True)
        for key, label in theme.accent_choices():
            act = QAction(label, accents)
            act.setCheckable(True)
            act.setChecked(theme.accent_name() == key)
            act.triggered.connect(lambda _=False, k=key: self.apply_theme(accent=k))
            accents.addAction(act)
            group.addAction(act)
            self._accent_actions[key] = act
        return menu

    def apply_theme(self, name: str | None = None, accent: str | None = None) -> None:
        """切主题 / 换强调色，并存进程序设置（下次启动照这个来）。"""
        if name is not None:
            theme.set_theme(name)
            self._settings.setValue("theme", theme.theme_name())
        if accent is not None:
            theme.set_accent(accent)
            self._settings.setValue("accent", theme.accent_name())
        theme.refresh()
        # 图标和页面投影都是位图 / 缓存，不重画不会跟着变
        self._refresh_glyphs()
        self.empty.refresh_icons()
        self.empty.update()
        self.viewer.viewport().update()
        for key, act in getattr(self, "_theme_actions", {}).items():
            act.setChecked(theme.theme_name() == key)
        for key, act in getattr(self, "_accent_actions", {}).items():
            act.setChecked(theme.accent_name() == key)

    def _shortcut_actions(self) -> dict:
        """快捷键表里那些项对应的 QAction。"""
        return {
            "open": self.act_open,
            "save": self.act_save,
            "save_as": self.act_save_as,
            "export": self.act_export,
            "close": self.act_close,
            "goto": self.act_goto,
            "prev": self.act_prev,
            "next": self.act_next,
            "zoom_in": self.act_zoom_in,
            "zoom_out": self.act_zoom_out,
            "fit_page": self.act_fit_page,
            "fit_width": self.act_fit_width,
            "actual": self.act_actual,
            "thumbs": self.act_thumbs,
            "toc": self.act_toc,
            "pipe_tree": self.act_pipe_tree,
            "undo": self.act_undo,
            "redo": self.act_redo,
            "delete": self.act_delete,
            "edit_text": self.act_edit_text,
            "copy": self.act_copy,
            "paste": self.act_paste,
            "lock": self.act_lock,
            "hide": self.act_hide,
            "show_all": self.act_show_all,
            "mode_text": self.act_mode_text,
            "mode_number": self.act_mode_number,
            "mode_overwrite": self.act_mode_overwrite,
            "mode_insert": self.act_mode_insert,
            "mode_select": self.act_mode_select,
            "match_data": self.act_match_data,
            "scan_all": self.act_scan_all,
            "material_scan": self.act_material_scan,
            "page_number": self.act_page_number,
            "magnifier": self.act_magnifier,
            "magnifier_new": self.act_magnifier_new,
            "magnifier_region": self.act_magnifier_region,
            "workbench": self.act_workbench,
            "add_to_board": self.act_add_to_board,
            "restart_number": self.act_restart_number,
            "eye_care": self.act_eye_care,
            "settings": self.act_settings,
        }

    def _setup_shortcuts(self) -> None:
        """按设置把快捷键套回 QAction（默认值跟以前一模一样）。"""
        self._tip_base = {}
        for key, action in self._shortcut_actions().items():
            tip = action.toolTip() or ""
            if tip.endswith("）") and "（" in tip:
                tip = tip.rsplit("（", 1)[0].rstrip()
            self._tip_base[key] = tip
        shortcutmap.apply(self._shortcut_actions(), self._settings)
        self._refresh_shortcut_tips()
        # 鼠标侧键：QAction 的 shortcut 表示不了鼠标键，自己收事件
        app = QApplication.instance()
        if app is not None and not getattr(self, "_mouse_filter_on", False):
            app.installEventFilter(self)
            self._mouse_filter_on = True

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt 命名
        """接鼠标侧键：按下侧键就触发它绑的那个功能（截图/别的程序里不管）。"""
        if event.type() == QEvent.Type.MouseButtonPress:
            button = event.button()
            which = ""
            if button == Qt.MouseButton.ExtraButton1:
                which = "mouse1"
            elif button == Qt.MouseButton.ExtraButton2:
                which = "mouse2"
            if which:
                # 放大窗上的侧键要留给「上一页/下一页」，
                # 不能顺手触发用户绑在侧键上的功能（比如翻页）。
                if self._is_magnifier_widget(obj):
                    return False
                key = shortcutmap.mouse_action(self._settings, which)
                action = self._shortcut_actions().get(key) if key else None
                if action is not None and action.isEnabled() and self.isActiveWindow():
                    action.trigger()
                    return True
        return super().eventFilter(obj, event)

    @staticmethod
    def _is_magnifier_widget(obj) -> bool:
        widget = obj if isinstance(obj, QWidget) else None
        while widget is not None:
            if isinstance(widget, MagnifierWindow):
                return True
            widget = widget.parentWidget()
        return False

    def _refresh_shortcut_tips(self) -> None:
        """提示语里写着的键跟着改，免得改了快捷键、说明里还是旧的。"""
        base_tips = getattr(self, "_tip_base", {})
        for key, action in self._shortcut_actions().items():
            base = base_tips.get(key) or ""
            text = shortcutmap.text_for(self._settings, key)
            if not base:
                continue
            action.setToolTip(f"{base}（{text}）" if text else base)

    def _bind_letter_keys(self) -> None:
        """给画布绑字母快捷键（V 浏览 / T 文字 / N 数字 / O 覆盖 / I 插队 / S 框选 / W 工作台 / R 重新从 1 编号）。

        为什么不直接写进 QAction 的 shortcut：那样是**窗口级**的，
        在搜索框、管线号改名框里打字会顺手把模式切走。绑到画布并且用
        WidgetWithChildrenShortcut，只有画布有焦点时才生效，两边都省心。
        """
        pairs = (
            ("V", self.act_mode_browse),
            ("T", self.act_mode_text),
            ("N", self.act_mode_number),
            ("O", self.act_mode_overwrite),
            ("I", self.act_mode_insert),
            ("S", self.act_mode_select),
            ("W", self.act_workbench),
            ("R", self.act_restart_number),
        )
        self._letter_keys = []
        for surface in (self.viewer, self.workbench.view):
            for key, action in pairs:
                shortcut = QShortcut(QKeySequence(key), surface)
                shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
                shortcut.activated.connect(action.trigger)
                self._letter_keys.append(shortcut)

    def _build_toolbar(self) -> None:
        """三段式工具栏：左边「文件」，中间「看图纸」（翻页/缩放/模式），右边「干活」。

        旧版是一条线上排了 20 多个控件、9 个纯文字按钮，读起来没有主次。
        现在改成分段槽（QFrame#toolSegment）+ 一个「⋯」溢出菜单：
        低频动作（强制刷新、重新编号、护眼）收进菜单，工具栏只留高频的。
        """
        bar = QToolBar("主工具栏", self)
        bar.setObjectName("mainToolBar")
        bar.setIconSize(QSize(18, 18))
        bar.setMovable(False)
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        bar.setFloatable(False)
        self.addToolBar(bar)

        def make(act, style=Qt.ToolButtonStyle.ToolButtonIconOnly) -> QToolButton:
            button = QToolButton()
            button.setDefaultAction(act)
            button.setToolButtonStyle(style)
            button.setIconSize(QSize(18, 18))
            button.setAutoRaise(True)
            if style is Qt.ToolButtonStyle.ToolButtonIconOnly:
                button.setProperty("compact", True)
            return button

        def segment(widgets: list) -> QFrame:
            """把几个控件装进一条浅底槽 —— 分组靠槽，不靠竖线。"""
            frame = QFrame()
            frame.setObjectName("toolSegment")
            inner = QHBoxLayout(frame)
            inner.setContentsMargins(2, 2, 2, 2)
            inner.setSpacing(2)
            for widget in widgets:
                inner.addWidget(widget)
            holder = QWidget()
            holder_layout = QHBoxLayout(holder)
            holder_layout.setContentsMargins(3, 0, 3, 0)
            holder_layout.addWidget(frame)
            bar.addWidget(holder)
            return frame

        def button_menu(button: QToolButton, actions: list) -> QMenu:
            """右键某个按钮弹出的菜单（左键照旧执行它自己的动作）。"""
            menu = QMenu(button)
            for item in actions:
                if item is None:
                    menu.addSeparator()
                else:
                    menu.addAction(item)
            button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            button.customContextMenuRequested.connect(
                lambda pos, m=menu, b=button: m.exec(b.mapToGlobal(pos)))
            return menu

        # 品牌区：图标 + 名字 + 版本（版本是淡色小字，不抢视线）-----
        icon = theme.app_icon()
        mark = QLabel()
        mark.setObjectName("brandMark")
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setFixedSize(30, 30)
        if not icon.isNull():
            mark.setPixmap(icon.pixmap(30, 30))
        else:
            mark.setText("Q")
        name = QLabel("雀雀 PDF")
        name.setObjectName("brandName")
        version = QLabel("v" + APP_VERSION)
        version.setObjectName("brandVersion")
        text_box = QVBoxLayout()
        text_box.setContentsMargins(0, 0, 0, 0)
        text_box.setSpacing(0)
        text_box.addWidget(name)
        text_box.addWidget(version)
        # 包一层 QWidget：窄屏时整块文字（名字 + 版本）一起让位，只留图标
        self._brand_text = QWidget()
        self._brand_text.setLayout(text_box)
        brand = QWidget()
        brand_box = QHBoxLayout(brand)
        brand_box.setContentsMargins(4, 0, 6, 0)
        brand_box.setSpacing(8)
        brand_box.addWidget(mark)
        brand_box.addWidget(self._brand_text)
        bar.addWidget(brand)

        # -- 文件：打开 / 保存 / 导出 + 撤销 / 重做 --------------------
        # 提取页面只在菜单里（工具栏已经挤了，而且它不是高频操作）
        segment([
            make(self.act_open),
            make(self.act_save),
            make(self.act_export),
            make(self.act_undo_tb),
            make(self.act_redo_tb),
        ])

        # -- 翻页 + 缩放：看图纸时手不离的那一排 ----------------------
        self._page_label = _PagePill("— / —")
        self._page_label.setObjectName("pagePill")
        self._page_label.setMinimumWidth(78)
        self._page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._page_label.setToolTip("双击输入页码跳转（Ctrl+G 也行）")
        self._page_label.double_clicked.connect(self._edit_page_number)
        segment([
            make(self.act_prev),
            make(self.act_next),
            self._page_label,
            make(self.act_zoom_out),
            make(self.act_zoom_in),
            make(self.act_fit_page),
            make(self.act_fit_width),
        ])

        # 双击页码后浮在上面的输入框，平时不显示
        self._page_editor = QLineEdit(self)
        self._page_editor.setObjectName("pageEditor")
        self._page_editor.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._page_editor.setToolTip("输入页码，回车跳转；Esc 取消")
        self._page_editor.hide()
        self._page_editor.returnPressed.connect(self._commit_page_number)
        self._page_editor.editingFinished.connect(self._commit_page_number)
        self._escape_page_editor = QShortcut(QKeySequence(Qt.Key.Key_Escape), self._page_editor)
        self._escape_page_editor.setContext(Qt.ShortcutContext.WidgetShortcut)
        self._escape_page_editor.activated.connect(self._cancel_page_edit)

        # -- 标注模式：浏览 / 文字 / 数字 / 框选 ------------------------
        # 「数字」一个按钮带三种模式（数字 / 覆盖 / 插队）：按钮上写当前那个，
        # 右键才弹出这三个的切换和编号设置 —— 低频动作不占工具栏的位置。
        self._number_btn = QToolButton()
        self._number_btn.setText("数字")
        self._number_btn.setCheckable(True)
        self._number_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._number_btn.setAutoRaise(True)
        self._number_btn.setToolTip(
            "数字标注（F2）\n右键：起始值与步长、重新从 1 编号、覆盖、插队")
        self._number_btn.clicked.connect(self._trigger_number_mode)
        self._number_menu = button_menu(
            self._number_btn,
            [
                self.act_number_settings,
                self.act_restart_number,
                None,
                self.act_mode_overwrite,
                self.act_mode_insert,
            ],
        )
        self._mode_buttons = [
            make(self.act_mode_browse, Qt.ToolButtonStyle.ToolButtonTextOnly),
            make(self.act_mode_text, Qt.ToolButtonStyle.ToolButtonTextOnly),
            self._number_btn,
            make(self.act_mode_select, Qt.ToolButtonStyle.ToolButtonTextOnly),
        ]
        self._mode_glyphs = ["select", "text", "number", "marquee"]
        self._mode_acts = [
            self.act_mode_browse, self.act_mode_text, self.act_mode_number, self.act_mode_select,
        ]
        segment(self._mode_buttons)

        # -- 干活：放大窗 / 材料表 / 数据匹配 --------------------------
        # 图标 + 短名（全名和说明在提示里，菜单里也是全名）
        self.act_magnifier.setIconText("放大窗")
        self.act_material_scan.setIconText("材料表")
        self.act_match_data.setIconText("数据匹配")
        magnifier_btn = make(self.act_magnifier, Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        magnifier_btn.setToolTip("放大窗（Ctrl+Shift+L）\n右键：新建放大窗、设置放大区域")
        self._magnifier_menu = button_menu(
            magnifier_btn, [self.act_magnifier_new, self.act_magnifier_region])
        material_btn = make(self.act_material_scan, Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        match_btn = make(self.act_match_data, Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._tool_buttons = [magnifier_btn, material_btn, match_btn]
        self._tool_actions = [self.act_magnifier, self.act_material_scan, self.act_match_data]
        segment(self._tool_buttons)

        # -- 右边：工作台（切视图）+ 溢出菜单 --------------------------
        gap = QWidget()
        gap.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bar.addWidget(gap)

        self._workbench_btn = make(self.act_workbench, Qt.ToolButtonStyle.ToolButtonTextOnly)
        more = QToolButton(self)
        more.setObjectName("overflowButton")
        more.setText("⋯")
        more.setToolTip("更多：强制刷新、护眼、显示全部")
        more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(more)
        menu.addAction(self.act_refresh)
        menu.addSeparator()
        menu.addAction(self.act_eye_care)
        menu.addAction(self.act_show_all)
        # 窗口极窄时「工作台」也挪进这里（挤不下就别硬塞）
        # 注意 PySide6 里 addAction()/addSeparator() 不返回 QAction，只能从
        # menu.actions() 里按位置取回来。
        menu.addSeparator()
        menu.addAction(self.act_workbench)
        items = menu.actions()
        self._overflow_sep = items[-2]
        self._overflow_workbench = items[-1]
        self._overflow_sep.setVisible(False)
        self._overflow_workbench.setVisible(False)
        more.setMenu(menu)
        # 工作台和 ⋯ 装进同一个容器再交给工具栏：直接 addWidget 两个，
        # 想单独藏掉工作台时 QToolBar 会把它重新显示出来。
        right = QWidget()
        right_box = QHBoxLayout(right)
        right_box.setContentsMargins(0, 0, 0, 0)
        right_box.setSpacing(2)
        right_box.addWidget(self._workbench_btn)
        right_box.addWidget(more)
        bar.addWidget(right)

        self._toolbar = bar
        self._toolbar_overflow = more
        self._tool_compact = None
        self._fit_toolbar()

    def _fit_toolbar(self) -> None:
        """按工具栏实际宽度分三档，别让「工作台」和「⋯」被挤出可视区。

        - 宽（≥1300px）：工具段显示"图标 + 短名"，和工作台、⋯ 一起排开。
        - 中（1150~1300）：三个工具按钮收成纯图标 —— 1280/1366 的笔记本都落这档。
        - 窄（<1150）：模式按钮也收成图标，工作台挪进 ⋯。
        """
        bar = getattr(self, "_toolbar", None)
        buttons = getattr(self, "_tool_buttons", None)
        if bar is None or not buttons or not bar.isVisible():
            return
        width = self.width()
        compact = width < 1360
        tight = width < 1240
        if getattr(self, "_tool_compact", None) == compact and getattr(
                self, "_tool_tight", None) == tight:
            return
        self._tool_compact = compact
        self._tool_tight = tight

        # 1) 工具段：图标 + 短名 → 纯图标
        actions = getattr(self, "_tool_actions", None) or []
        for index, button in enumerate(buttons):
            if compact:
                # 光设 IconOnly 不够：带 defaultAction 的按钮，中文标签宽度
                # 照样算进 sizeHint（73px vs 只有图标的 39px），所以连宽度一起钉住。
                button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
                button.setFixedWidth(34)
                if index < len(actions):
                    button.setToolTip(actions[index].iconText())
            else:
                button.setMinimumWidth(0)
                button.setMaximumWidth(16777215)
                button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
                if index < len(actions):
                    button.setText(actions[index].iconText())
                    button.setToolTip(actions[index].toolTip())

        # 2) 极窄：模式按钮纯图标，工作台进 ⋯
        mode_buttons = getattr(self, "_mode_buttons", None) or []
        mode_glyphs = getattr(self, "_mode_glyphs", None) or []
        mode_acts = getattr(self, "_mode_acts", None) or []
        for index, button in enumerate(mode_buttons):
            glyph = mode_glyphs[index] if index < len(mode_glyphs) else ""
            if tight:
                button.setIcon(self._glyph(glyph))
                button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
                button.setFixedWidth(34)
                if index < len(mode_acts):
                    button.setToolTip(mode_acts[index].text())
            else:
                button.setIcon(QIcon())
                button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
                button.setMinimumWidth(0)
                button.setMaximumWidth(16777215)
                if index < len(mode_acts):
                    button.setToolTip(mode_acts[index].toolTip())
        workbench_btn = getattr(self, "_workbench_btn", None)
        if workbench_btn is not None:
            workbench_btn.setVisible(not tight)
        brand_text = getattr(self, "_brand_text", None)
        if brand_text is not None:
            brand_text.setVisible(not tight)
        for action in (getattr(self, "_overflow_sep", None),
                       getattr(self, "_overflow_workbench", None)):
            if action is not None:
                action.setVisible(tight)
        bar.updateGeometry()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_toolbar()

    def showEvent(self, event) -> None:
        # 首次显示时窗口可能正好就是最终尺寸，不会有 resizeEvent，
        # 这里补一次，窄屏一打开就是紧凑工具栏。
        super().showEvent(event)
        self._fit_toolbar()

    def _connect(self) -> None:
        self._tabbar.currentChanged.connect(self._on_tab_changed)
        self._tabbar.tabCloseRequested.connect(self._close_tab_requested)
        self._tabbar.tabMoved.connect(self._on_tab_moved)
        self._new_tab.clicked.connect(self.open_dialog)
        self.viewer.page_changed.connect(self._on_page_changed)
        self.viewer.zoom_changed.connect(self._on_zoom_changed)
        self.viewer.selection_changed.connect(self._on_selection_changed)
        self.viewer.mode_changed.connect(self._on_mode_changed)
        self.viewer.message.connect(self._show_message)
        self.viewer.region_picked.connect(self._on_region_picked)
        self.viewer.insert_requested.connect(self._on_insert_requested)
        # 数字标注模式下右键 → 直接打开「起始值与步长」
        self.viewer.number_settings_requested.connect(self.number_settings_dialog)
        self.thumbs.page_selected.connect(self.viewer.goto_page)
        self.thumbs.board_requested.connect(self._add_page_to_board)
        self.toc.page_clicked.connect(self.viewer.goto_page)
        self.pipe_tree.page_selected.connect(self._goto_from_pipe_tree)
        self.pipe_tree.scan_requested.connect(self.open_scan_dialog)
        self.pipe_tree.export_requested.connect(self.export_by_pipe_tag)
        self.pipe_tree.import_requested.connect(self.import_outline_into_tree)
        self.pipe_tree.merge_requested.connect(self.merge_toc_with_bookmarks)
        self.pipe_tree.clear_requested.connect(self.clear_directory_tree)
        self.pipe_tree.tag_renamed.connect(self._rename_pipe_tag)
        self.props.style_changed.connect(self._on_style_changed)
        self.props.page_style_changed.connect(self._on_page_style_changed)
        self.props.page_mode_changed.connect(self._on_page_mode_changed)

        # 工作台与单页视图共用同一套状态（标注 / 编号 / 撤销栈）
        self.workbench.message.connect(self._show_message)
        self.workbench.view.mode_changed.connect(self._on_mode_changed)
        self.workbench.view.selection_changed.connect(self._on_selection_changed)

        self.undo_stack.indexChanged.connect(self._refresh_style_from_selection)
        for signal in (self.store.added, self.store.removed, self.store.changed, self.store.cleared):
            signal.connect(self._refresh_title)
            signal.connect(self._refresh_mode_hint)
        self.sequence.changed.connect(self._refresh_mode_hint)

        self.project.changed.connect(self._on_project_changed)
        self.project.notice.connect(self._show_message)
        self.project.mismatch.connect(self._on_project_mismatch)

        self.export_manager.started.connect(self._on_export_started)
        self.export_manager.progress.connect(self._on_export_progress)
        self.export_manager.finished.connect(self._on_export_finished)
        self.export_manager.failed.connect(self._on_export_failed)
        self.export_manager.cancelled.connect(self._on_export_cancelled)
        self.extract_manager.started.connect(self._on_extract_started)
        self.extract_manager.progress.connect(self._on_extract_progress)
        self.extract_manager.finished.connect(self._on_extract_finished)
        self.extract_manager.failed.connect(self._on_extract_failed)
        self.extract_manager.cancelled.connect(self._on_extract_cancelled)

    # ------------------------------------------------------------------ 文档

    def open_dialog(self) -> None:
        start = self._settings.value("last_dir", os.path.expanduser("~"), type=str)
        path, _ = QFileDialog.getOpenFileName(self, "打开 PDF", start, "PDF 文件 (*.pdf);;所有文件 (*.*)")
        if path:
            self.open_pdf(path)

    def open_pdf(self, path: str, project_path: str = "") -> None:
        """打开一份 PDF。

        ``project_path`` 只在「从 .qpdf 打开」时给：那份 PDF 可能已经在别的标签
        里用**另一个**项目文件打开了，这时候不能悄悄切过去——用户以为打开的是
        B 项目，实际在写 A 项目，改动就落到错的地方。
        """
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            QMessageBox.warning(self, "打开失败", f"文件不存在：\n{path}")
            return
        if self.export_manager.is_busy or self.extract_manager.is_busy:
            QMessageBox.information(self, "正在导出", "导出还没结束，完成后才能打开别的文档。")
            return
        # 同一个 PDF 只开一个会话：再点一次就切到那个标签，不重复读 1.2 GB
        existing = self._find_session(path)
        if existing is not None:
            if project_path and existing.project.is_open:
                want = os.path.normcase(os.path.abspath(project_path))
                have = os.path.normcase(os.path.abspath(existing.project.path))
                if want != have:
                    self._activate_session(existing)
                    QMessageBox.information(
                        self,
                        "这份 PDF 已经用另一个项目打开了",
                        f"「{os.path.basename(path)}」现在开在标签「{existing.title}」里，"
                        f"用的是项目文件：\n{have}\n\n"
                        f"这次要打开的是：\n{want}\n\n"
                        "一份 PDF 同时只能挂一个项目文件，已经切到那个标签了。"
                        "要用另一个项目，先把它那个标签关掉再打开。",
                    )
                    return
            self._activate_session(existing)
            self._show_message(f"{os.path.basename(path)} 已经打开了，已切到那个标签页")
            return

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        self.status.showMessage(f"正在打开 {os.path.basename(path)}…")
        QApplication.processEvents()
        try:
            doc = PdfDocument(path)
            # 大文件读页尺寸要几秒，期间让用户看到进度而不是以为卡死了
            sizes = []
            for i in range(doc.page_count):
                sizes.append(doc.page_size(i))
                if i and i % 200 == 0:
                    self.status.showMessage(f"正在读取页面尺寸… {i}/{doc.page_count}")
                    QApplication.processEvents()
        except PdfEngineError as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "打开失败", str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "打开失败", f"{type(exc).__name__}: {exc}")
            return
        finally:
            if QApplication.overrideCursor() is not None:
                QApplication.restoreOverrideCursor()

        ds = DocumentSession(path, doc, sizes, self)
        # 「从 .qpdf 打开」时，项目文件可能不在 PDF 旁边；把指定路径带进会话，
        # 等这个标签第一次激活时 attach 到它，而不是按同目录同名去另找。
        if project_path:
            ds.forced_project_path = os.path.abspath(project_path)
        # 新会话的项目库位置跟着主窗口走：设置里改过目录、或者调用方（测试）
        # 在建窗后改过，都不能让新标签偷偷跑回默认目录去建库。
        try:
            ds.project.set_project_dir(self.project.project_dir())
        except Exception:  # noqa: BLE001 - 目录配置读不回来就用默认的
            pass
        self._sessions.append(ds)
        # 新标签也按当前设置来（缓存上限、渲染线程）
        mb = self._settings.value("cache_mb", 0, type=int) or 0
        if mb > 0:
            ds.render_manager.set_budget_mb(mb)
        threads = self._settings.value("render_threads", 0, type=int)
        if threads:
            ds.render_manager.set_threads(threads)
        self._tabbar.blockSignals(True)
        self._tabbar.addTab(ds.title)
        self._tabbar.setTabToolTip(self._tabbar.count() - 1, ds.tooltip())
        self._tabbar.setVisible(True)
        self._tabbar.blockSignals(False)
        self._activate_session(ds)

        self.status.clearMessage()
        self._settings.setValue("last_dir", os.path.dirname(path))
        self._push_recent(path)
        self._update_ui_state()

    # ------------------------------------------------------------- 多标签页

    def _find_session(self, path: str) -> DocumentSession | None:
        target = os.path.normcase(os.path.abspath(path))
        for ds in self._sessions:
            if os.path.normcase(ds.path) == target:
                return ds
        return None

    def _find_session_by_project(self, project_path: str) -> DocumentSession | None:
        target = os.path.normcase(os.path.abspath(project_path))
        for ds in self._sessions:
            if ds.project.is_open and os.path.normcase(ds.project.path) == target:
                return ds
        return None

    def _sync_tab_index(self) -> None:
        if self._active is None:
            return
        try:
            index = self._sessions.index(self._active)
        except ValueError:
            return
        if self._tabbar.currentIndex() != index:
            self._tabbar.blockSignals(True)
            self._tabbar.setCurrentIndex(index)
            self._tabbar.blockSignals(False)

    def _on_tab_changed(self, index: int) -> None:
        if 0 <= index < len(self._sessions):
            self._activate_session(self._sessions[index])

    def _on_tab_moved(self, from_index: int, to_index: int) -> None:
        if from_index == to_index:
            return
        if not (0 <= from_index < len(self._sessions) and 0 <= to_index < len(self._sessions)):
            return
        ds = self._sessions.pop(from_index)
        self._sessions.insert(to_index, ds)
        self._refresh_tab_titles()

    def _refresh_tab_titles(self) -> None:
        for index, ds in enumerate(self._sessions):
            if index >= self._tabbar.count():
                break
            title = ("● " if ds.dirty else "") + ds.title
            if self._tabbar.tabText(index) != title:
                self._tabbar.setTabText(index, title)
            self._tabbar.setTabToolTip(index, ds.tooltip())

    def _save_view_state(self) -> None:
        ds = self._active
        if ds is None:
            return
        viewer = self.viewer
        ds.state.update(
            {
                "page": viewer.current_page,
                "zoom": viewer.zoom,
                "hscroll": viewer.horizontalScrollBar().value(),
                "vscroll": viewer.verticalScrollBar().value(),
                "mode": viewer.mode,
                "left": self._left_tabs.currentIndex(),
                "workbench": bool(self.act_workbench.isChecked()),
            }
        )

    def _restore_view_state(self, ds: DocumentSession) -> None:
        st = ds.state
        if st["workbench"]:
            self.act_workbench.blockSignals(True)
            self.act_workbench.setChecked(True)
            self.act_workbench.blockSignals(False)
            self.set_workbench_visible(True)
        else:
            if st["zoom"] > 0:
                self.viewer.clear_fit_mode()
                self.viewer.set_zoom(float(st["zoom"]))
            # 只按页恢复位置：滚动条的像素值和倍率绑在一起，
            # 换个倍率再套旧像素值会落到别的页上（比不恢复还糟）。
            self.viewer.goto_page(int(st["page"]))
        if st["mode"] is not None:
            try:
                self.viewer.set_mode(st["mode"])
            except ValueError:
                pass
        self._left_tabs.setCurrentIndex(int(st["left"]))

    def _wire_session(self, ds: DocumentSession) -> None:
        ds.undo_stack.indexChanged.connect(self._refresh_style_from_selection)
        ds.undo_stack.canUndoChanged.connect(self._refresh_undo_actions)
        ds.undo_stack.canRedoChanged.connect(self._refresh_undo_actions)
        ds.undo_stack.undoTextChanged.connect(self._refresh_undo_actions)
        ds.undo_stack.redoTextChanged.connect(self._refresh_undo_actions)
        for signal in (ds.store.added, ds.store.removed, ds.store.changed, ds.store.cleared):
            signal.connect(self._refresh_title)
            signal.connect(self._refresh_mode_hint)
            signal.connect(self._refresh_magnifier_annotations)
        ds.sequence.changed.connect(self._refresh_mode_hint)
        ds.sequence.changed.connect(self._refresh_magnifier_annotations)
        ds.project.changed.connect(self._on_project_changed)
        ds.project.notice.connect(self._show_message)
        ds.project.mismatch.connect(self._on_project_mismatch)
        ds.project.backup_done.connect(self._on_backup_done)
        ds.project.crash_suspect.connect(self._on_crash_suspect)

    def _unwire_session(self, ds: DocumentSession) -> None:
        pairs = [
            (ds.undo_stack.indexChanged, self._refresh_style_from_selection),
            (ds.undo_stack.canUndoChanged, self._refresh_undo_actions),
            (ds.undo_stack.canRedoChanged, self._refresh_undo_actions),
            (ds.undo_stack.undoTextChanged, self._refresh_undo_actions),
            (ds.undo_stack.redoTextChanged, self._refresh_undo_actions),
        ]
        for signal in (ds.store.added, ds.store.removed, ds.store.changed, ds.store.cleared):
            pairs.append((signal, self._refresh_title))
            pairs.append((signal, self._refresh_mode_hint))
            pairs.append((signal, self._refresh_magnifier_annotations))
        pairs.append((ds.sequence.changed, self._refresh_mode_hint))
        pairs.append((ds.sequence.changed, self._refresh_magnifier_annotations))
        pairs.append((ds.project.changed, self._on_project_changed))
        pairs.append((ds.project.notice, self._show_message))
        pairs.append((ds.project.mismatch, self._on_project_mismatch))
        pairs.append((ds.project.backup_done, self._on_backup_done))
        pairs.append((ds.project.crash_suspect, self._on_crash_suspect))
        for signal, slot in pairs:
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass

    def _activate_session(self, ds: DocumentSession) -> None:
        if ds is self._active:
            self._sync_tab_index()
            return
        self._save_view_state()
        if self._active is not None:
            old = self._active
            self._close_session_dialogs(old)
            self._unwire_session(old)
            old.release_cache()
            self.workbench.detach()
            self.thumbs.clear_document()
            # 放大窗不再跟着标签藏起来：用户开着它就是要一直看那块地方，
            # 换个标签只换内容（下面 set_document + refresh）。
            if self.act_workbench.isChecked():
                self.act_workbench.blockSignals(True)
                self.act_workbench.setChecked(False)
                self.act_workbench.blockSignals(False)
            self._stack.setCurrentWidget(self.viewer)

        self._active = ds
        self._doc = ds.doc
        self.render_manager = ds.render_manager
        self.store = ds.store
        self.undo_stack = ds.undo_stack
        self.sequence = ds.sequence
        self.boards = ds.boards
        self.project = ds.project

        self.viewer.rebind(ds.render_manager, ds.store, ds.undo_stack, ds.sequence)
        self.thumbs.set_render_manager(ds.render_manager)
        ds.ensure_render_doc()
        self._apply_render_budget()
        self.viewer.set_document(ds.sizes)
        self.thumbs.set_document(ds.doc, ds.sizes)
        self.workbench.attach(
            ds.page_sizes(),
            ds.render_manager,
            ds.store,
            ds.sequence,
            ds.undo_stack,
            ds.boards,
        )
        self.magnifiers.set_document(ds.path)
        self._load_toc()
        self._wire_session(ds)
        if not ds.attached:
            ds.attached = True
            self._attach_project(ds.doc)
        else:
            self._load_scan_region()
            self._load_annot_region()
            self._load_magnifier_region()
            self._load_pipe_index()
            self._on_project_changed()
        self.act_backup_restore.setText("备份与还原(&R)…")
        self._sync_tab_index()
        self._refresh_tab_titles()
        self._refresh_undo_actions()
        self._restore_view_state(ds)
        # 切回来要重新发一次绘制请求：非活动标签的排队任务早就作废了，
        # 而窗口尺寸没变的话 Qt 不会主动重画，屏幕就一直是白的。
        if ds.render_manager.cache_bytes == 0:
            self.viewer.force_refresh()
        else:
            self.viewer.refresh_tiles()
        # 放大窗是全局的：换文档只换内容，窗口本身不藏
        self._refresh_magnifier_annotations()
        self.magnifiers.show_saved()
        self._refresh_magnifier(self.viewer.current_page)
        self.magnifiers.raise_all()
        self._set_act_magnifier_checked(self.magnifiers.any_visible())
        self._update_ui_state()

    def _close_session_dialogs(self, ds: DocumentSession) -> None:
        """把「属于这个会话」的识别/匹配窗口收掉。

        这两个窗口手里握着某个 ProjectManager 的引用，会话一关（项目库 close）
        它们还活着，再点保存就会写进一个已经关掉的库。
        """
        if self._scan_dialog is not None and self._scan_dialog_owner is ds:
            self._close_scan_dialog()
        if self._match_dialog is not None and self._match_dialog_owner is ds:
            dialog, self._match_dialog = self._match_dialog, None
            self._match_dialog_owner = None
            try:
                dialog.close()
            finally:
                dialog.deleteLater()
        if self._material_dialog is not None and self._material_dialog_owner is ds:
            self._close_material_dialog()
        if self._page_dialog is not None and self._page_dialog_owner is ds:
            self._close_page_dialog()

    def _cycle_tab(self, delta: int) -> None:
        """Ctrl+Tab / Ctrl+Shift+Tab：在标签之间循环。"""
        count = len(self._sessions)
        if count < 2:
            return
        idx = self._tabbar.currentIndex()
        if idx < 0:
            idx = 0
        self._activate_session(self._sessions[(idx + delta) % count])

    def _close_tab_requested(self, index: int) -> None:
        if 0 <= index < len(self._sessions):
            self._close_session(self._sessions[index])

    def _close_session(self, ds: DocumentSession, ask: bool = True) -> bool:
        """关一个标签。返回 False 表示用户取消了。"""
        if self.export_manager.is_busy or self.extract_manager.is_busy:
            QMessageBox.information(self, "正在导出", "导出还没结束，完成后才能关闭这个文档。")
            return False
        if ask and ds.dirty:
            self._activate_session(ds)
            answer = self._ask_save_on_close(ds)
            if answer == "cancel":
                return False
            if answer == "discard":
                ds.project.discard_unsaved()
            else:
                try:
                    ds.project.save()
                except ProjectError as exc:
                    if not self._confirm_close_after_save_failure(ds, exc):
                        return False

        was_active = ds is self._active
        self._close_session_dialogs(ds)
        try:
            index = self._sessions.index(ds)
        except ValueError:
            return False
        if was_active:
            self._unwire_session(ds)
            self._active = None
            self.workbench.detach()
            self.thumbs.clear_document()
            self.viewer.clear_document()
            self.magnifiers.hide_all()
            self._doc = None
            self.pipe_tree.clear()
            self.toc.clear()
            self._toc_nodes = ()
        self._sessions.pop(index)
        self._tabbar.blockSignals(True)
        self._tabbar.removeTab(index)
        self._tabbar.blockSignals(False)
        ds.dispose()

        if was_active:
            if self._sessions:
                self._activate_session(self._sessions[min(index, len(self._sessions) - 1)])
            else:
                self._reset_after_last_tab()
        self._refresh_tab_titles()
        return True

    def _reset_after_last_tab(self) -> None:
        self._tabbar.setVisible(False)
        self.viewer.clear_document()
        # 刚关掉的那个会话已经 dispose（项目库 close、渲染池 shutdown）了。
        # 这几个字段原来还指着它，`store.clear()`、`project.set_project_dir()`
        # 之类的后续调用全打在已经关掉的对象上，所以这里换一套新的空对象。
        #
        # 换之前先把旧对象上的"运行时设置"接过来：项目库/备份目录可能是本进程里
        # 临时改过的（测试直接用 set_project_dir 指到临时目录，设置面板改完也会
        # 先落到这里），只读 QSettings 会把它们丢掉，再打开同一份图纸就接到
        # 默认目录里的另一个项目文件上去——标注"少了几条"或干脆串成别人的。
        old_project = self.project
        project_override = getattr(old_project, "_project_override", "") or self._settings.value(
            "project_dir", "", type=str
        )
        backup_override = getattr(old_project, "_backup_override", "") or self._settings.value(
            "backup_dir", "", type=str
        )
        # 旧撤销栈马上要被换掉，它上面还挂着我们的刷新槽：不先断开，栈被销毁后
        # 迟到的 canUndoChanged/cleanChanged 会打到已经没了的 C++ 对象上
        # （RuntimeError: Internal C++ object (QUndoStack) already deleted）。
        old_stack = getattr(self, "undo_stack", None)
        if old_stack is not None:
            for signal, slot in (
                (old_stack.indexChanged, self._refresh_style_from_selection),
                (old_stack.canUndoChanged, self._refresh_undo_actions),
                (old_stack.canRedoChanged, self._refresh_undo_actions),
                (old_stack.undoTextChanged, self._refresh_undo_actions),
                (old_stack.redoTextChanged, self._refresh_undo_actions),
            ):
                try:
                    signal.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
        self.render_manager = RenderManager(self)
        self.store = AnnotationStore(self)
        self.undo_stack = QUndoStack(self)
        self.sequence = SequenceManager(self.store, self)
        self.boards = BoardStore(self)
        self.project = ProjectManager(self.store, self.sequence, self)
        self.project.bind_boards(self.boards)
        self.project.set_project_dir(project_override)
        self.project.set_backup_dir(backup_override)
        # 新的空状态也要接上刷新槽，否则"关掉最后一个标签后"撤销/重做按钮和标题不再更新。
        self.undo_stack.indexChanged.connect(self._refresh_style_from_selection)
        self.undo_stack.canUndoChanged.connect(self._refresh_undo_actions)
        self.undo_stack.canRedoChanged.connect(self._refresh_undo_actions)
        self.undo_stack.undoTextChanged.connect(self._refresh_undo_actions)
        self.undo_stack.redoTextChanged.connect(self._refresh_undo_actions)
        for signal in (self.store.added, self.store.removed, self.store.changed, self.store.cleared):
            signal.connect(self._refresh_title)
            signal.connect(self._refresh_mode_hint)
        self.sequence.changed.connect(self._refresh_mode_hint)
        self.project.changed.connect(self._on_project_changed)
        self.project.notice.connect(self._show_message)
        self.project.mismatch.connect(self._on_project_mismatch)
        self.viewer.rebind(self.render_manager, self.store, self.undo_stack, self.sequence)
        self.workbench.detach()
        self.thumbs.set_render_manager(self.render_manager)
        self.pipe_tree.clear()
        self.toc.clear()
        self._toc_nodes = ()
        self.magnifiers.set_document("")
        self.magnifiers.set_annotations(())
        self.magnifiers.hide_all()
        self._set_act_magnifier_checked(False)
        self._close_material_dialog()
        self._close_page_dialog()
        self.material_region = None
        self.viewer.set_material_region(None)
        self.page_number_region = None
        self.page_numbers = None
        self.viewer.set_page_number(None)
        self.setWindowTitle("QPDF — 雀雀 PDF")
        self.status.set_save_state("—")
        self.status.set_page(0, 0)
        self._doc = None
        self._active = None
        self._update_ui_state()

    def close_document(self, silent: bool = False) -> None:
        """关掉当前标签（保留其它标签）。"""
        if self._active is None:
            return
        if not self._close_session(self._active, ask=not silent):
            return


    # ------------------------------------------------------------- 工作台

    def _surface(self):
        """当前在用的画布：单页视图 or 工作台。"""
        if self.act_workbench.isChecked() and self._doc is not None:
            return self.workbench.view
        return self.viewer

    def _fit_view(self) -> None:
        if self._surface() is self.viewer:
            self.viewer.fit_page()
        else:
            self.workbench.view.fit_all()

    def set_workbench_visible(self, on: bool) -> None:
        if on and self._doc is None:
            self.act_workbench.blockSignals(True)
            self.act_workbench.setChecked(False)
            self.act_workbench.blockSignals(False)
            self._show_message("先打开一个 PDF，再用工作台")
            return
        self._stack.setCurrentIndex(1 if on else 0)
        # 覆盖 / 插队 / 框选只在单页视图里有意义
        for act in (self.act_mode_overwrite, self.act_mode_insert, self.act_mode_select):
            act.setEnabled(not on and self._doc is not None)
        self.viewer.set_mode(ToolMode.BROWSE)
        self.workbench.view.set_mode(ToolMode.BROWSE)
        if on:
            self.workbench.view.refresh_board()
            self.workbench.view.fit_all()
        else:
            # 回单页视图：把"工作台在看的页"这个过滤撤掉，否则主界面里
            # 不在卡片上的页全被判成"翻走了"，请求直接作废 —— 表现为一片空白。
            self.render_manager.clear_focus_pages()
            self.workbench.view.release_pixmaps()
            freed = self.render_manager.drop_previews()
            self.viewer.refresh_tiles()
            if freed > 0:
                self._show_message(f"已释放工作台预览缓存 {freed / 1048576:.0f} MB")
        self._refresh_mode_hint()
        self.props.set_style(self._surface().default_style, selection_count=0)

    def add_current_page_to_board(self) -> None:
        if self._doc is None:
            return
        self.workbench.add_page(self.viewer.current_page)
        if not self.act_workbench.isChecked():
            self.act_workbench.setChecked(True)
        else:
            self.workbench.view.refresh_board()
            self.workbench.view.auto_layout()

    # ------------------------------------------------------------------ 设置

    def _app_dir(self) -> str:
        """程序所在目录（绿色版 = exe 旁边，开发时 = 项目根）。"""
        if getattr(sys, "frozen", False):
            return os.path.dirname(os.path.abspath(sys.executable))
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def export_dir(self) -> str:
        """导出文件夹：设置里填了就用，否则用程序目录下的 输出/。"""
        path = self._settings.value("export_dir", "", type=str).strip()
        return path or default_export_dir()

    def backup_dir(self) -> str:
        path = self._settings.value("backup_dir", "", type=str).strip()
        return path or default_backup_dir()

    def _ensure_dir(self, path: str) -> str:
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            pass
        return path

    def _apply_render_budget(self) -> None:
        mb = self._settings.value("cache_mb", 0, type=int) or 0
        # 没设过就按 CPU 核数算（默认值随机器走，而不是钉死 3 个线程）
        threads = self._settings.value("render_threads", 0, type=int)
        backup = self._settings.value("backup_dir", "", type=str)
        managers = [self.render_manager]
        for ds in self._sessions:
            if ds.render_manager not in managers:
                managers.append(ds.render_manager)
        for manager in managers:
            if mb > 0:
                manager.set_budget_mb(mb)
            if threads:
                manager.set_threads(threads)
        self.project.set_backup_dir(backup)

    def open_settings(self) -> None:
        dialog = SettingsDialog(
            self.render_manager.budget_mb,
            self._settings.value("export_dir", "", type=str),
            self._settings.value("backup_dir", "", type=str),
            self._settings.value("project_dir", "", type=str),
            self._settings.value("match_skip_duplicates", False, type=bool),
            self.render_manager.threads,
            shortcutmap.load_all(self._settings),
            {key: shortcutmap.mouse_action(self._settings, key) for key, _ in shortcutmap.MOUSE_KEYS},
            self,
        )
        if dialog.exec() != SettingsDialog.DialogCode.Accepted:
            return
        self._settings.setValue("cache_mb", dialog.cache_mb_value())
        self._settings.setValue("export_dir", dialog.export_dir_value())
        self._settings.setValue("backup_dir", dialog.backup_dir_value())
        self._settings.setValue("project_dir", dialog.project_dir_value())
        self._settings.setValue("match_skip_duplicates", dialog.skip_duplicates_value())
        self._settings.setValue("render_threads", dialog.threads_value())
        shortcutmap.save(self._settings, dialog.shortcut_values())
        shortcutmap.save_mouse(self._settings, dialog.mouse_values())
        shortcutmap.apply(self._shortcut_actions(), self._settings)
        self._apply_render_budget()
        self.project.set_backup_dir(dialog.backup_dir_value())
        # 项目库目录对所有标签页一致：已经开着的会话也一起换过去
        new_dir = dialog.project_dir_value()
        self.project.set_project_dir(new_dir)
        for ds in self._sessions:
            try:
                ds.project.set_project_dir(new_dir)
            except Exception:  # noqa: BLE001 - 单个会话换不了不影响别的
                pass
        self._refresh_shortcut_tips()
        self._show_message(
            "设置已保存：缓存 {} MB · 渲染线程 {} · 导出到 {}".format(
                dialog.cache_mb_value(), dialog.threads_value(), self.export_dir()
            )
        )

    # ------------------------------------------------------------- 项目文件

    # ------------------------------------------------------------------ 编辑

    def _undo(self) -> None:
        stack = getattr(self, "undo_stack", None)
        if stack is not None:
            stack.undo()

    def _redo(self) -> None:
        stack = getattr(self, "undo_stack", None)
        if stack is not None:
            stack.redo()

    def _refresh_undo_actions(self, *args) -> None:
        stack = getattr(self, "undo_stack", None)
        if stack is None:
            return
        try:
            can_undo = stack.canUndo()
            can_redo = stack.canRedo()
            undo_text = stack.undoText()
            redo_text = stack.redoText()
        except RuntimeError:
            # 栈的 C++ 对象已经没了（会话销毁、程序退出），这种刷新本来就是多余的。
            return
        self.act_undo.setEnabled(can_undo)
        self.act_undo_tb.setEnabled(can_undo)
        self.act_redo.setEnabled(can_redo)
        self.act_redo_tb.setEnabled(can_redo)
        self.act_undo.setText(f"撤销 {undo_text}" if undo_text else "撤销")
        self.act_redo.setText(f"重做 {redo_text}" if redo_text else "重做")
        self.act_undo_tb.setToolTip(f"撤销 {undo_text} (Ctrl+Z)" if undo_text else "撤销 (Ctrl+Z)")
        self.act_redo_tb.setToolTip(f"重做 {redo_text} (Ctrl+Y)" if redo_text else "重做 (Ctrl+Y)")

    def _attach_project(self, doc: PdfDocument) -> None:
        """打开 PDF 后，同目录如果有同名 .qpdf 就自动接着上次的标注。"""
        forced = ""
        if self._active is not None:
            forced = self._active.forced_project_path
            self._active.forced_project_path = ""
        status = self.project.attach(doc, forced)
        if status == "loaded":
            self.undo_stack.clear()
            self._show_message(f"已载入项目：{os.path.basename(self.project.path)}")
        elif status == "none":
            self._show_message("还没有项目文件，第一次标注时会自动创建 .qpdf")
        elif status == "mismatch":
            self._handle_project_mismatch(self.project.last_mismatch)
        elif status == "broken":
            self._handle_project_broken()
        self._load_scan_region()
        self._load_pipe_index()
        self._on_project_changed()

    def _handle_project_mismatch(self, info: dict) -> None:
        """项目对不上时让用户自己决定，绝不静默覆盖旧项目。"""
        choice = self._ask_project_mismatch(info)
        if choice not in ("continue", "continue_remember"):
            self._show_message(
                "没有加载项目文件；原 .qpdf 没有被改动。若继续标注，会新建另一个项目文件，不会覆盖它。"
            )
            return
        remember = choice == "continue_remember"
        backup = self.project.load_mismatched(info, remember=remember)
        if not backup:
            QMessageBox.warning(
                self,
                "项目文件对不上",
                "这次没有继续加载。原始项目文件仍然原样保留。\n\n"
                f"{self.project.last_error or '继续加载失败。'}",
            )
            return
        self.undo_stack.clear()
        out_of_range = 0
        if self._doc is not None:
            out_of_range = sum(1 for ann in self.store.all() if ann.page >= self._doc.page_count)
        message = (
            f"已继续载入对不上的项目：{os.path.basename(self.project.path)}\n"
            f"原项目已自动备份为：{os.path.basename(backup)}\n\n"
            "如果标注位置明显对不上，请关闭这份文件，用备份恢复原项目。"
        )
        if remember:
            message += "\n\n已记住这份 PDF 的指纹，以后打开不再提示对不上。"
        if out_of_range:
            message += f"\n\n注意：有 {out_of_range} 条标注的页号超出当前 PDF，已保留但不会显示。"
        QMessageBox.information(self, "已继续加载项目", message)
        self._show_message(f"已继续加载项目；原文件已备份为 {os.path.basename(backup)}")

    def _handle_project_broken(self) -> None:
        """同名 .qpdf 存在但读不了：只提示和保护，绝不新建/覆盖。"""
        path = self.project.last_error_path or self.project.path
        reason = self.project.last_error or "项目文件无法读取"
        self._show_message(
            "项目文件打不开；原文件没有被修改，也不会被覆盖。"
        )
        while True:
            if self._ask_project_broken(path, reason) == "locate":
                self._show_project_location(path)
                continue
            return

    def _ask_project_broken(self, path: str, reason: str) -> str:
        """坏项目提示；单独抽出来便于离屏测试替换。返回 locate 或 close。"""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle("项目文件无法读取")
        box.setText("找到了同名项目文件，但它打不开。")
        box.setInformativeText(
            f"{reason}\n\n"
            f"{path}\n\n"
            "原 .qpdf 不会被改写或删除。请先备份/检查这个文件；"
            "如果继续标注，会另建一个新项目文件，不会覆盖它。"
        )
        location_btn = box.addButton(
            "查看项目位置", QMessageBox.ButtonRole.ActionRole
        )
        box.addButton("关闭", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        return "locate" if box.clickedButton() is location_btn else "close"

    def _ask_project_mismatch(self, info: dict) -> str:
        """三选一：继续加载 / 取消加载 / 查看项目位置。返回 continue 或 cancel。

        单独抽成方法，离屏测试可以替换它，避免模态框把测试卡住。
        """
        stored = info.get("stored") or {}
        actual = info.get("actual") or {}
        path = info.get("path", "")
        stored_fp = str(stored.get("fingerprint", "") or "")
        actual_fp = str(actual.get("fingerprint", "") or "")
        same_pages = int(stored.get("page_count", -1)) == int(actual.get("page_count", -2))
        reason = "页数不一致" if not same_pages else "文件内容指纹不一致"
        while True:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("项目文件和当前 PDF 对不上")
            box.setText(
                f"{reason}。项目文件记录的 PDF 和现在打开的不是同一份。\n"
                "继续加载可能把旧标注画到错的页上。"
            )
            box.setInformativeText(
                f"项目文件：{path}\n"
                f"记录指纹：{stored_fp[:16] or '（空）'}…\n"
                f"当前指纹：{actual_fp[:16] or '（空）'}…\n"
                f"记录页数：{stored.get('page_count', '?')}，"
                f"当前页数：{actual.get('page_count', '?')}\n"
                f"记录大小：{stored.get('size', '?')} 字节，"
                f"当前大小：{actual.get('size', '?')} 字节\n\n"
                "选择「继续加载」会先自动备份原 .qpdf，然后才打开它；"
                "选择「取消加载」不会改动原文件。"
            )
            remember_box = QCheckBox("记住这份 PDF（以后不再提示）")
            if same_pages:
                remember_box.setChecked(True)
            else:
                remember_box.setEnabled(False)
                remember_box.setText("页数不同，不能记住这份 PDF")
            box.setCheckBox(remember_box)
            continue_btn = box.addButton("继续加载", QMessageBox.ButtonRole.AcceptRole)
            cancel_btn = box.addButton("取消加载", QMessageBox.ButtonRole.RejectRole)
            view_btn = box.addButton("查看项目位置", QMessageBox.ButtonRole.ActionRole)
            box.setDefaultButton(cancel_btn)
            box.exec()
            clicked = box.clickedButton()
            if clicked is view_btn:
                self._show_project_location(path)
                continue
            if clicked is continue_btn:
                if remember_box.isChecked() and remember_box.isEnabled():
                    return "continue_remember"
                return "continue"
            return "cancel"

    def _show_project_location(self, path: str) -> None:
        if not path:
            return
        folder = os.path.dirname(path)
        if folder and os.path.isdir(folder):
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
        else:
            QMessageBox.information(self, "项目位置", f"项目文件：\n{path}")

    def _on_project_changed(self, *args) -> None:
        self._refresh_title()
        self._refresh_save_state()
        self._refresh_tab_titles()
        self._update_ui_state()

    def _refresh_save_state(self) -> None:
        if self._doc is None:
            self.status.set_save_state("—")
            self._saved_flash = False
            return
        if not self.project.exists:
            self.status.set_save_state("无项目")
            return
        if self.project.is_dirty:
            # 自动保存一直在写项目文件，所以这里的"未保存"指的是
            # "自上次手动保存以来又有改动"，不是"硬盘上没有"。说法必须一致，
            # 否则左边闪一句"已保存到项目文件"、右边写着"未保存"，用户没法判断。
            self.status.set_save_state(
                "● 有改动",
                "改动已经自动写进项目文件了；按 Ctrl+S 锁存一份，"
                "退出时选「不保存」才会回到这次锁存的状态。",
            )
            self._saved_flash = False
            return
        stamp = self.project.last_saved_at
        text = (
            time.strftime("✓ 已保存 %H:%M", time.localtime(stamp))
            if stamp
            else "✓ 已保存"
        )
        self.status.set_save_state(text, "项目文件已是最新（自动保存 + 手动保存都落过盘）。")
        # 刚落盘那一瞬间在状态栏闪一句，省得用户老盯着右边小字
        if not self._saved_flash and len(self.store):
            self._saved_flash = True
            self.statusBar().showMessage(f"已保存 {len(self.store)} 条标注到项目文件", 1600)

    def _on_project_mismatch(self, info: dict) -> None:
        """只记录，不在这里弹窗：attach() 返回后由 _attach_project 统一询问。

        这样「继续加载」可以先把原 .qpdf 备份，再把它打开；旧实现只提示
        “没有载入”，用户没有安全的继续入口。
        """
        self._last_project_mismatch = dict(info or {})

    def save_project(self) -> None:
        if self._doc is None:
            return
        try:
            written = self.project.save()
        except ProjectError as exc:
            # 保存失败必须让用户看见：以前这里静默 return，用户以为存上了
            QMessageBox.warning(
                self,
                "保存失败",
                f"{exc}\n\n标注还在内存里，先别关窗口。可以换一个项目库位置"
                "（设置 → 项目库）或腾出磁盘空间后再按 Ctrl+S 重试。",
            )
            return
        self._on_project_changed()
        if written:
            self.status.showMessage(f"已保存 {written} 项改动到项目文件", 3000)

    def save_project_as(self) -> None:
        if not self.project.is_open:
            self.save_project()
            if not self.project.is_open:
                return
        start = os.path.dirname(self.project.path)
        path, _ = QFileDialog.getSaveFileName(
            self, "项目另存为", start, "QPDF 项目 (*.qpdf)"
        )
        if not path:
            return
        if not path.lower().endswith(".qpdf"):
            path += ".qpdf"
        if self.project.save_as(path):
            self._show_message(f"项目已另存为：{os.path.basename(path)}")
        self._on_project_changed()

    def register_qpdf_assoc(self) -> None:
        """把 .qpdf 关联到本程序（只写 HKCU，不用管理员）。"""
        from core import assoc

        if not assoc.is_available():
            QMessageBox.information(self, "关联文件", "这个功能只在 Windows 上有效")
            return
        if assoc.is_registered() and assoc.registered_command() == assoc.command_line():
            self._show_message(".qpdf 已经关联到本程序了，不用重复设置")
            return
        try:
            message = assoc.register()
        except assoc.AssocError as exc:
            QMessageBox.critical(self, "关联失败", str(exc))
            return
        self.act_unassoc.setEnabled(True)
        QMessageBox.information(
            self,
            "关联完成",
            message + "\n\n如果资源管理器里的图标还没变，注销一次或重启资源管理器即可。",
        )

    def unregister_qpdf_assoc(self) -> None:
        from core import assoc

        if not assoc.is_registered():
            self._show_message(".qpdf 本来就没有关联")
            return
        if assoc.unregister():
            self._show_message("已取消 .qpdf 关联")
        else:
            QMessageBox.warning(self, "取消关联", "没删掉，可能被别的东西占用着")

    def open_project_dialog(self) -> None:
        start = self._settings.value("last_project_dir", "", type=str)
        path, _ = QFileDialog.getOpenFileName(
            self, "打开项目", start, "QPDF 项目 (*.qpdf)"
        )
        if not path:
            return
        self._settings.setValue("last_project_dir", os.path.dirname(path))
        self.open_project(path)

    def _locate_project_pdf(self, source_path: str) -> str:
        """项目里的原始 PDF 不在本机时，让用户选本机同内容的 PDF。

        单独抽出来便于离屏测试替换，避免模态框/文件对话框把测试卡住。
        """
        QMessageBox.information(
            self,
            "找不到原始 PDF",
            f"项目记录的原始 PDF 不在本机：\n{source_path or '（没有记录）'}\n\n"
            "如果这是从别的电脑拷来的项目，请选择本机上同一套图纸的 PDF。\n"
            "程序会先核对内容；对不上时会再问你是否继续，并先备份项目文件。",
        )
        start = self._settings.value("last_dir", "", type=str)
        if not start and source_path:
            start = os.path.dirname(source_path)
        chosen, _ = QFileDialog.getOpenFileName(
            self, "选择这份项目对应的 PDF", start, "PDF (*.pdf)"
        )
        return chosen or ""

    def open_project(self, project_path: str) -> bool:
        """从 .qpdf 找到源 PDF 并打开（第 9.3 节：项目里不存 PDF 本体）。"""
        from database.repository import ProjectError, ProjectRepository

        repo = ProjectRepository(project_path)
        try:
            repo.open()
            source = repo.source_info()
        except ProjectError as exc:
            QMessageBox.critical(self, "打开项目失败", str(exc))
            repo.close()
            return False
        repo.close()

        source_path = source.get("path", "")
        if not source_path or not os.path.isfile(source_path):
            # 项目是从另一台电脑拷来的：里面记的 E:\... 本机没有。
            # 不要拒绝，也不要按同目录同名去乱找——让用户选本机这份图纸，
            # 选完再按内容指纹决定是直接挂接还是提示“对不上”。
            source_path = self._locate_project_pdf(source_path)
            if not source_path:
                return False
        # 这个项目已经开在某个标签里了：直接切过去，绝不复制出第二个编辑会话
        already = self._find_session_by_project(project_path)
        if already is not None:
            self._activate_session(already)
            self._show_message(f"这个项目已经打开了，已切到「{already.title}」")
            return True
        # 同一个 PDF 被另一个项目用着也不允许：一个 PDF 只能有一个编辑会话
        if self._doc is not None and len(self.store):
            if not self.project.is_open:
                self.project.ensure_project()
            try:
                self.project.save()
            except ProjectError as exc:
                QMessageBox.warning(
                    self,
                    "打开项目失败",
                    f"当前文档还有改动没存进项目文件：\n{exc}\n\n"
                    "为了避免丢掉这些标注，这次先不打开别的项目。处理完再试一次。",
                )
                return False
        self.open_pdf(source_path, project_path=project_path)
        return True

    def check_project_source(self) -> None:
        """检查旁边的 .qpdf 是不是属于这份 PDF。"""
        if self._doc is None:
            QMessageBox.information(self, "检查项目", "先打开一份 PDF 吧。")
            return
        info = self.project.check_correspondence()
        state = info.get("state")
        text = info.get("message", "")
        if state == "ok":
            QMessageBox.information(self, "检查项目", text)
        elif state == "none":
            QMessageBox.information(self, "检查项目", text)
        else:
            QMessageBox.warning(self, "检查项目", text)

    def backup_project(self) -> None:
        if not self.project.exists:
            self.save_project()
        target = self.project.backup()
        if target:
            self._show_message(f"已备份到 备份/{os.path.basename(target)}")

    def open_backup_dialog(self) -> None:
        """备份列表 + 还原（第 16 章）。"""
        if self._doc is None:
            return
        if not self.project.is_open and not self.project.ensure_project():
            QMessageBox.information(self, "备份与还原", "这个文档还没有项目文件，标注之后才会自动建。")
            return
        dialog = BackupDialog(self.project, self)
        dialog.exec()

    def _on_backup_done(self, path: str) -> None:
        self._show_message(f"已自动备份：{os.path.basename(path)}")

    def _on_crash_suspect(self, project_path: str) -> None:
        """上次没正常退出：**不弹模态框**，改成状态栏 + 菜单里的入口。

        打开文档是高频操作，在这里弹一个"要不要看备份"的模态框，会让批量开标签、
        脚本化打开都被卡住（测试里会直接把它挂死）；提示放在状态栏一样看得见。
        """
        name = os.path.basename(project_path)
        self._show_message(
            f"「{name}」上次没有正常退出：标注停在最后一次自动保存的位置，"
            "需要的话用「文件 → 备份与还原」找回更完整的版本"
        )
        self.status.set_save_state(
            "● 上次异常退出",
            "上次没有正常关闭。标注一般还在最后一次自动保存里；"
            "「文件 → 备份与还原」可以看备份并还原。",
        )
        self.act_backup_restore.setText("备份与还原（建议查看）…")

    def open_output_dir(self) -> None:
        folder = self.project.output_dir()
        if not folder or not os.path.isdir(folder):
            self._show_message("还没有输出目录（有项目文件之后才建）")
            return
        os.startfile(folder)  # noqa: S606 - Windows 上打开资源管理器

    # ------------------------------------------------------------------ 导出

    def export_pdf(self) -> None:
        """第 11 章：把标注导出成 PDF（后台线程 + 临时文件 + 写后校验）。"""
        if self._doc is None:
            return
        if self.export_manager.is_busy:
            self._show_message("上一次导出还没结束，请稍候")
            return
        annotations = [ann for ann in self.store.all() if not ann.hidden]
        has_page_numbers = self.page_numbers is not None and self.page_numbers.enabled
        if not annotations and not has_page_numbers:
            QMessageBox.information(
                self, "导出 PDF", "当前文档还没有标注或页码，先标几个/设页码再导出。"
            )
            return

        source = self._doc.path
        stem = os.path.splitext(os.path.basename(source))[0]
        # 默认落在设置里的导出文件夹（默认是程序目录下的 输出/，不往桌面丢）
        default_target = os.path.join(self._ensure_dir(self.export_dir()), f"{stem}_已标注.pdf")

        plan_preview = build_plan(source, default_target, self._doc.page_count, self.store, self.sequence)
        bookmarks = self._export_toc()
        dialog = ExportDialog(
            source,
            default_target,
            self._doc.page_count,
            plan_preview.items,
            bool(bookmarks),
            self,
        )
        if dialog.exec() != ExportDialog.DialogCode.Accepted:
            return

        plan = build_plan(
            source,
            dialog.path(),
            self._doc.page_count,
            self.store,
            self.sequence,
            flatten=dialog.flatten(),
        )
        # 「写进书签」从目录树面板搬到这里：勾了才写，不勾就保持原 PDF 的书签
        plan.toc = bookmarks if dialog.with_toc() else []
        # 页码单独走 ExportPlan.page_numbers，不混进标注 items；写进 PDF 内容流
        plan.page_numbers = self.page_numbers
        self._export_open_folder = dialog.open_folder()
        if not self.export_manager.start(plan):
            self._show_message("导出没能启动，请重试")

    # ------------------------------------------------------------------ 提取

    def extract_pages(self) -> None:
        """F04：把挑出来的页面另存成新 PDF（标注、书签一起带）。"""
        if self._doc is None:
            return
        if self.extract_manager.is_busy:
            self._show_message("上一次提取还没结束，请稍候")
            return

        source = self._doc.path
        stem = os.path.splitext(os.path.basename(source))[0]
        default_target = os.path.join(self._ensure_dir(self.export_dir()), f"{stem}_提取.pdf")

        toc = self._export_toc()
        visible = [ann for ann in self.store.all() if not ann.hidden]
        dialog = ExtractDialog(
            source,
            default_target,
            self._doc.page_count,
            current_page=self.viewer.current_page,
            annot_count=len(visible),
            bookmark_count=len(toc),
            outline=self._toc_nodes,
            project_open=self.project.is_open,
            page_numbers_enabled=bool(
                self.page_numbers is not None and self.page_numbers.enabled
            ),
            parent=self,
        )
        if dialog.exec() != ExtractDialog.DialogCode.Accepted:
            return

        plan = build_extract_plan(
            source,
            dialog.path(),
            dialog.pages(),
            self.store,
            self.sequence,
            toc=toc if dialog.keep_toc() else [],
            keep_annotations=dialog.keep_annotations(),
            page_numbers=self.page_numbers if dialog.keep_page_numbers() else None,
            source_page_count=self._doc.page_count,
        )
        self._extract_open_folder = dialog.open_folder()
        # 项目子集要等 PDF 真写成功之后再落盘，先把「存哪、哪几页」记着
        self._pending_subset = (
            (dialog.qpdf_path(), list(dialog.pages()))
            if dialog.want_qpdf()
            else None
        )
        if not self.extract_manager.start(plan):
            self._pending_subset = None
            self._show_message("提取没能启动，请重试")

    def export_by_pipe_tag(self, preselect: str = "") -> None:
        """F06：按管线号把相关页面提成一个 PDF（标注、书签跟着走）。"""
        if self._doc is None:
            return
        if self.extract_manager.is_busy:
            self._show_message("上一次提取/导出还没结束，请稍候")
            return
        index = self.pipe_tree.index()
        if index.tag_count == 0:   # 是 property，不是方法
            QMessageBox.information(
                self,
                "按编号导出",
                "目录树还是空的。\n\n先框一个识别区域、跑一次「批量识别管线号」，"
                "等目录树里出现编号了再按编号导出。",
            )
            return

        source = self._doc.path
        stem = os.path.splitext(os.path.basename(source))[0]
        suffix = f"_{safe_filename(preselect)}" if preselect else "_按编号"
        default_target = os.path.join(self._ensure_dir(self.export_dir()), f"{stem}{suffix}.pdf")
        toc = self._export_toc()
        dialog = ExportTagDialog(
            index,
            default_target,
            keep_toc_default=bool(toc),
            preselect=preselect,
            parent=self,
        )
        if dialog.exec() != ExportTagDialog.DialogCode.Accepted:
            return

        tags = dialog.tags()
        pages = dialog.pages()
        if not tags or not pages:
            self._show_message("没选编号，或者选中的编号没有对应页码")
            return
        target = dialog.path()
        if os.path.normcase(target) == os.path.normcase(source):
            QMessageBox.warning(self, "按编号导出", "输出文件不能和原始 PDF 同名。")
            return

        plan = build_extract_plan(
            source,
            target,
            pages,
            self.store,
            self.sequence,
            toc=toc if dialog.keep_toc() else [],
            keep_annotations=True,
            page_numbers=self.page_numbers,
            source_page_count=self._doc.page_count,
        )
        # 这条路不带项目子集（那是 F04 挑页提取的选项）
        self._pending_subset = None
        self._extract_open_folder = False
        if not self.extract_manager.start(plan):
            self._show_message("导出没能启动，请重试")
            return
        self._show_message(
            f"正在按 {len(tags)} 个编号提取 {len(pages)} 页：{'、'.join(tags[:3])}"
            + ("…" if len(tags) > 3 else "")
        )

    def _on_extract_started(self) -> None:
        self.act_extract.setEnabled(False)
        self.status.set_export_progress(0, 1)
        self._show_message("正在提取页面…")

    def _on_extract_progress(self, done: int, total: int, message: str) -> None:
        self.status.set_export_progress(done, max(1, total))
        if message:
            self._show_message(message)

    def _on_extract_finished(self, result) -> None:
        self.act_extract.setEnabled(self._doc is not None)
        self.status.clear_export_progress()
        self._show_message(
            f"已提取 {result.page_count} 页 → {os.path.basename(result.target)}"
            f"（{result.size / 1048576:.1f} MB，用时 {result.elapsed:.1f} s）"
        )
        self._write_pending_subset()
        if getattr(self, "_extract_open_folder", False):
            folder = os.path.dirname(result.target)
            if folder and os.path.isdir(folder):
                os.startfile(folder)  # noqa: S606

    def _write_pending_subset(self) -> None:
        """提取成功后，把这几页的标注另存成一份 .qpdf。"""
        pending, self._pending_subset = getattr(self, "_pending_subset", None), None
        if not pending:
            return
        target, pages = pending
        if not self.project.is_open:
            return
        try:
            count = self.project.export_subset(target, pages, note="提取页面时带出的子集")
        except Exception as exc:  # noqa: BLE001 - 子集失败不该连累已经提取好的 PDF
            from core import crashlog

            crashlog.log_exception(exc, "提取页面：写项目子集")
            self._show_message(f"PDF 已提取，但项目子集没写成功：{exc}")
            return
        self._show_message(
            f"已提取页面，并另存项目子集 {os.path.basename(target)}（{count} 条标注）"
        )

    def _on_extract_failed(self, message: str) -> None:
        self._pending_subset = None
        self.act_extract.setEnabled(self._doc is not None)
        self.status.clear_export_progress()
        QMessageBox.critical(self, "提取失败", message)

    def _on_extract_cancelled(self) -> None:
        self._pending_subset = None
        self.act_extract.setEnabled(self._doc is not None)
        self.status.clear_export_progress()
        self._show_message("提取已取消，临时文件已清理")

    # -- 区域识别 / 书签（Phase 10）--------------------------------------
    def _stored_bookmarks(self) -> list:
        from core.bookmarks import Bookmark

        raw = self.project.get_setting("bookmarks", None) or []
        marks = []
        for item in raw:
            if isinstance(item, dict):
                marks.append(Bookmark.from_dict(item))
        return marks

    def _export_toc(self) -> list:
        from core.bookmarks import to_pymupdf_toc

        marks = self._stored_bookmarks()
        if not marks and self.project.is_open:
            rows = self.project.load_scan_rows()
            tags = {p: r.get("tag", "") for p, r in rows.items()}
            from core.bookmarks import build_bookmarks

            marks = build_bookmarks(tags)
        return to_pymupdf_toc(marks) if marks else []

    def open_scan_dialog(self) -> None:
        """批量识别（第 Phase 10 章）：区域 + 页码范围 → 后台线程扫。"""
        if self._doc is None:
            return
        if self.scan_region is None:
            answer = QMessageBox.question(
                self,
                "还没有识别区域",
                "先框一个识别区域吗？\n\n区域就是每页上「管线号」所在的那一小块位置，"
                "软件按同样的比例去每一页的同一个地方读字。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.viewer.set_mode(ToolMode.REGION)
            return
        if self.project.is_open is False:
            self.project.ensure_project()
        if self._scan_dialog is not None:
            if self._scan_dialog_owner is self._active:
                self._scan_dialog.show()
                self._scan_dialog.raise_()
                self._scan_dialog.activateWindow()
                return
            # 旧对话框挂的是别的标签的项目，留着它就会把识别结果写进错的地方
            self._close_scan_dialog()
        dialog = ScanDialog(self._doc.path, self.scan_region, self._doc.page_count, self.project, self)
        dialog.bookmarks_ready.connect(self._on_bookmarks_ready)
        self._scan_dialog = dialog
        self._scan_dialog_owner = self._active
        dialog.show()

    def _close_scan_dialog(self) -> None:
        """收掉批量识别窗口：先让它的扫描线程停下来，再关。"""
        dialog, self._scan_dialog = self._scan_dialog, None
        self._scan_dialog_owner = None
        if dialog is None:
            return
        try:
            dialog.stop_scan()
        except Exception:  # noqa: BLE001 - 关窗口不该因为停扫描失败而卡住
            pass
        try:
            dialog.close()
        finally:
            dialog.deleteLater()

    def open_material_dialog(self) -> None:
        """材料表识别：区域 + 页码范围 → 逐页 OCR → 汇总 Excel。"""
        if self._doc is None:
            return
        if self.material_region is None:
            answer = QMessageBox.question(
                self,
                "还没有材料表区域",
                "先框一个材料表区域吗？\n\n材料表通常在图框右上角，"
                "软件按同样的比例去每一页的同一个地方读。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self._start_region_pick("material")
            return
        if self.project.is_open is False:
            self.project.ensure_project()
        if self._material_dialog is not None and self._material_dialog_owner is self._active:
            self._material_dialog.show()
            self._material_dialog.raise_()
            self._material_dialog.activateWindow()
            return
        if self._material_dialog is not None:
            self._close_material_dialog()
        dialog = MaterialDialog(
            self._doc.path,
            self.material_region,
            self._doc.page_count,
            self.project,
            self,
        )
        self._material_dialog = dialog
        self._material_dialog_owner = self._active
        dialog.show()

    def _close_material_dialog(self) -> None:
        """收掉材料表窗口：先停后台线程，再关。"""
        dialog, self._material_dialog = self._material_dialog, None
        self._material_dialog_owner = None
        if dialog is None:
            return
        try:
            dialog.stop_scan()
        except Exception:  # noqa: BLE001
            pass
        try:
            dialog.close()
        finally:
            dialog.deleteLater()

    # ---------------------------------------------------------------- 页码

    def open_page_number_dialog(self) -> None:
        if self._doc is None:
            return
        if not self.project.is_open:
            self.project.ensure_project()
        region = self.page_number_region
        if region is None and self.page_numbers is not None:
            region = self.page_numbers.region
        if region is None:
            answer = QMessageBox.question(
                self,
                "还没有页码位置",
                "先框一个页码位置吗？\n\n所有页都会在同一个比例位置写页码。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self._start_region_pick("page_number")
            return
        if self._page_dialog is not None and self._page_dialog_owner is self._active:
            self._page_dialog.show()
            self._page_dialog.raise_()
            self._page_dialog.activateWindow()
            return
        if self._page_dialog is not None:
            self._close_page_dialog()
        spec = self.page_numbers
        if spec is None:
            spec = PageNumberSpec(region=region, style=self.props.style())
        elif spec.region is None:
            spec = PageNumberSpec(region, spec.template, spec.style, spec.enabled)
        dialog = PageNumberDialog(spec, self._doc.page_count, self)
        dialog.region_requested.connect(
            lambda d=dialog: self._on_page_number_region_requested(d)
        )
        dialog.accepted.connect(self._apply_page_dialog)
        dialog.finished.connect(self._on_page_dialog_finished)
        self._page_dialog = dialog
        self._page_dialog_owner = self._active
        dialog.show()

    def _on_page_number_region_requested(self, dialog) -> None:
        if dialog is not self._page_dialog:
            return
        dialog.hide()
        self._start_region_pick("page_number")

    def _apply_page_dialog(self) -> None:
        dialog = self._page_dialog
        if dialog is None:
            return
        spec = dialog.result_spec()
        if spec is None:
            return
        self.page_numbers = spec
        self.page_number_region = spec.region
        self.viewer.set_page_number(spec)
        self.project.set_setting("page_number", spec.to_dict())
        self.project.set_setting("page_number_region", spec.region.to_dict())
        self.props.set_page_mode(True)
        self.props.set_style(spec.style, 0)
        self._update_ui_state()
        self._refresh_mode_hint()
        self._show_message("页码已设置：导出/打印的 PDF 会写在框选位置")

    def _on_page_dialog_finished(self, _result: int) -> None:
        dialog, self._page_dialog = self._page_dialog, None
        self._page_dialog_owner = None
        if dialog is not None:
            dialog.deleteLater()

    def _close_page_dialog(self) -> None:
        dialog, self._page_dialog = self._page_dialog, None
        self._page_dialog_owner = None
        if dialog is not None:
            dialog.close()
            dialog.deleteLater()

    def clear_page_numbers(self) -> None:
        self.page_numbers = None
        self.page_number_region = None
        self.viewer.set_page_number(None)
        if self.project.is_open:
            self.project.set_setting("page_number", None)
            self.project.set_setting("page_number_region", None)
        self.props.set_page_mode(False)
        self._update_ui_state()
        self._refresh_mode_hint()
        self._show_message("页码已清除")

    def _on_page_style_changed(self, style: AnnotationStyle) -> None:
        if self.page_numbers is None:
            return
        self.page_numbers = self.page_numbers.with_style(style)
        self.viewer.set_page_number(self.page_numbers)
        if self.project.is_open:
            self.project.set_setting("page_number", self.page_numbers.to_dict())

    def _on_page_mode_changed(self, on: bool) -> None:
        if not on:
            return
        if self.page_numbers is None:
            self.props.set_page_mode(False)
            self._show_message("还没有页码：先用「插入/设置页码…」建一个")
            return
        self.props.set_style(self.page_numbers.style, 0)

    def _on_bookmarks_ready(self, marks) -> None:
        self._show_message(f"已生成 {len(marks)} 条书签，导出 PDF 时会写进去")        # 目录跟着识别结果一起更新（F01）；顺手把新标签并进左侧目录树
        self._load_pipe_index()
        if self._doc is not None:
            self._load_toc()
        self._left_tabs.setCurrentWidget(self.pipe_tree)

    def _on_export_started(self) -> None:
        self.act_export.setEnabled(False)
        self.status.set_export_progress(0, 1)
        self._show_message("正在导出 PDF…（原始文件不会被改动）")

    def _on_export_progress(self, done: int, total: int, message: str) -> None:
        self.status.set_export_progress(done, total)
        if message:
            self._show_message(f"{message}（{done}/{total}）")

    def _on_export_finished(self, result) -> None:
        self.status.clear_export_progress()
        self.act_export.setEnabled(self._doc is not None)
        size_mb = result.size / 1024 / 1024
        summary = (
            f"已导出 {result.annot_count} 个标注到\n{result.target}\n\n"
            f"形式：{'扁平化' if result.flatten else '标准注释'}（{result.mode}）\n"
            f"耗时 {result.elapsed:.1f} 秒 ｜ {size_mb:.1f} MB ｜ {result.page_count} 页\n"
            f"原始 PDF 未改动。"
        )
        self._show_message(f"导出完成：{os.path.basename(result.target)}")
        QMessageBox.information(self, "导出完成", summary)
        if getattr(self, "_export_open_folder", False):
            folder = os.path.dirname(result.target)
            if os.path.isdir(folder):
                os.startfile(folder)  # noqa: S606

    def _on_export_failed(self, message: str) -> None:
        self.status.clear_export_progress()
        self.act_export.setEnabled(self._doc is not None)
        QMessageBox.critical(self, "导出失败", f"{message}\n\n原始 PDF 没有被改动。")

    def _on_export_cancelled(self) -> None:
        self.status.clear_export_progress()
        self.act_export.setEnabled(self._doc is not None)
        self._show_message("导出已取消，临时文件已清理")

    # ------------------------------------------------------------------ 状态

    def _update_ui_state(self) -> None:
        has_doc = self._doc is not None
        self.act_scan_all.setEnabled(has_doc)
        self.act_material_scan.setEnabled(has_doc)
        self.act_material_region.setEnabled(has_doc)
        self.act_page_number.setEnabled(has_doc)
        self.act_page_number_region.setEnabled(has_doc)
        self.act_page_number_clear.setEnabled(has_doc and self.page_numbers is not None)
        self.act_pipe_tree.setEnabled(has_doc)
        self.act_merge_toc.setEnabled(has_doc)
        for act in (
            self.act_close,
            self.act_zoom_in,
            self.act_zoom_out,
            self.act_fit_page,
            self.act_fit_width,
            self.act_actual,
            self.act_prev,
            self.act_next,
            self.act_goto,
            self.act_single,
            self.act_mode_text,
            self.act_mode_number,
            self.act_mode_overwrite,
            self.act_mode_insert,
            self.act_mode_select,
            self.act_restart_number,
            self.act_number_settings,
            self.act_radius_custom,
            self.act_save,
            self.act_save_as,
            self.act_backup,
            self.act_export,
            self.act_workbench,
            self.act_add_to_board,
            self.act_board_pages,
            self.act_annot_region,
            self.act_match_data,
        ):
            act.setEnabled(has_doc)
        self.act_output_dir.setEnabled(has_doc and self.project.exists)
        if getattr(self, "_number_btn", None) is not None:
            # 数字按钮代表「数字 / 覆盖 / 插队」，能用与否跟着这三个动作
            self._number_btn.setEnabled(self.act_mode_number.isEnabled())
            self._sync_number_button()
        if has_doc and self._surface() is self.workbench.view:
            # 工作台里覆盖 / 插队 / 框选用不上
            for act in (self.act_mode_overwrite, self.act_mode_insert, self.act_mode_select):
                act.setEnabled(False)
        if has_doc:
            self.setWindowTitle(f"{os.path.basename(self._doc.path)} — QPDF")
        self._refresh_title()
        self._refresh_page_label()
        self._sync_empty_state(has_doc)

    def _sync_empty_state(self, has_doc: bool) -> None:
        """没有文档就把空状态页顶上来；打开文档后它自动让位给画布。"""
        if not has_doc:
            self.empty.set_recent(self._recent_files())
            # 关掉文档后状态栏别留着上一份的倍率
            self.status.set_zoom(1.0)
            if self._stack.currentWidget() is not self.empty:
                self._stack.setCurrentWidget(self.empty)
        elif self._stack.currentWidget() is self.empty:
            self._stack.setCurrentWidget(self.viewer)

    def _refresh_title(self, *args) -> None:
        """标题栏带 * 表示还有改动没写进 .qpdf。"""
        if self._doc is None:
            self.setWindowTitle("QPDF — 雀雀 PDF")
            return
        mark = " *" if self.project.is_dirty else ""
        self.setWindowTitle(f"{os.path.basename(self._doc.path)}{mark} — QPDF")
        self.act_unhide_all.setEnabled(self.store.hidden_count > 0)

    # ------------------------------------------------------------------ 交互

    def goto_page_dialog(self) -> None:
        if self._doc is None:
            return
        current = self.viewer.current_page + 1
        page, ok = QInputDialog.getInt(
            self, "跳转页码", f"页码（1 – {self._doc.page_count}）：", current, 1, self._doc.page_count, 1
        )
        if ok:
            self.viewer.goto_page(page - 1)

    def _on_page_changed(self, index: int) -> None:
        if self._doc is None:
            return
        self.status.set_page(index, self._doc.page_count)
        self.thumbs.set_current_page(index)
        self._refresh_page_label()
        if self.magnifiers.windows:
            self._refresh_magnifier(index)

    def _on_zoom_changed(self, zoom: float) -> None:
        self.status.set_zoom(zoom)
        # 放大窗是独立窗口，缩放画布不用动它；只顺手把可贴的位置刷新一下
        if self.magnifiers.windows:
            self.magnifiers.set_snap_targets(self._magnifier_snap_targets())

    # ------------------------------------------------------------------ 标注

    MODE_LABEL = {
        ToolMode.BROWSE: "浏览",
        ToolMode.TEXT: "文字",
        ToolMode.NUMBER: "数字",
        ToolMode.OVERWRITE: "覆盖",
        ToolMode.INSERT: "插队",
        ToolMode.SELECT: "框选",
        ToolMode.REGION: "识别区域",
    }

    def _sync_number_button(self, mode=None) -> None:
        """「数字」按钮跟着模式走：文字写「数字 / 覆盖 / 插队」，亮不亮也一起管。"""
        button = getattr(self, "_number_btn", None)
        if button is None:
            return
        mode = mode if mode is not None else self._surface().mode
        label = {ToolMode.OVERWRITE: "覆盖", ToolMode.INSERT: "插队"}.get(mode, "数字")
        if button.text() != label:
            button.setText(label)
        button.blockSignals(True)
        button.setChecked(mode in (ToolMode.NUMBER, ToolMode.OVERWRITE, ToolMode.INSERT))
        button.blockSignals(False)

    def _trigger_number_mode(self) -> None:
        """左键点「数字」：回到数字模式（覆盖 / 插队只从右键菜单进）。"""
        if self._surface().mode is not ToolMode.NUMBER:
            self._surface().set_mode(ToolMode.NUMBER)
        self._sync_mode_ui()

    def _sync_mode_ui(self) -> None:
        """工具栏的模式段对齐到「当前画布」的真实模式。

        工作台和主画布各有一份 mode：后台那份自己重载时也会 emit mode_changed
        （比如插队插完一条，工作台跟着刷新成 BROWSE），照它的信号改按钮就会
        出现「浏览亮着、数字也亮着」。所以统一以 _surface().mode 为准。
        """
        if not hasattr(self, "_mode_actions") or not hasattr(self, "status"):
            return
        surface = self._surface()
        mode = surface.mode
        self._sync_number_button(mode)
        if mode is ToolMode.REGION:
            act = {
                "annot": self.act_annot_region,
                "magnifier": self.act_magnifier_region,
                "material": self.act_material_region,
                "page_number": self.act_page_number_region,
            }.get(self._region_target, self.act_scan_region)
        else:
            act = self._mode_actions.get(mode)
        if act is not None and not act.isChecked():
            act.setChecked(True)
        self._refresh_mode_hint()

    def _on_mode_changed(self, mode) -> None:
        sender = self.sender()
        if sender is not None and sender is not self._surface():
            # 不是当前画布发出的（后台画布自己重载），别拿它改工具栏
            return
        self._sync_mode_ui()
        # 属性面板跟着切到该模式"新建标注"的默认样式（数字和文字各一套）
        self._refresh_style_from_selection()
        self._surface().setFocus(Qt.FocusReason.OtherFocusReason)

    def _refresh_mode_hint(self, *args) -> None:
        """状态栏的模式栏（第 12.3 节：当前模式 + 当前编号）。"""
        surface = self._surface()
        mode = surface.mode
        text = self.MODE_LABEL.get(mode, "浏览")
        if mode is ToolMode.NUMBER:
            text = f"{text} · 下一个 {self.sequence.next_text()}"
        elif mode is ToolMode.OVERWRITE:
            text = f"{text} · 点旧数字改位置"
        elif mode is ToolMode.INSERT:
            text = f"{text} · 点要插入的位置"
        elif mode is ToolMode.SELECT:
            text = f"{text} · 拖框选标注，Del 删除"
        elif mode is ToolMode.REGION:
            active_magnifier = self._active_magnifier()
            regions = {
                "annot": self.annot_region,
                "magnifier": active_magnifier.region if active_magnifier is not None else None,
                "material": self.material_region,
                "page_number": self.page_number_region,
            }
            region = regions.get(self._region_target, self.scan_region)
            what = {
                "annot": "标注位置",
                "magnifier": "放大区域",
                "material": "材料表",
                "page_number": "页码位置",
            }.get(self._region_target, "管线号")
            if region is None:
                text = f"{text} · 拖一个矩形把{what}框起来"
            else:
                text = (
                    f"{text} · 已框 左{region.x0 * 100:.0f}% 上{region.y0 * 100:.0f}% "
                    f"右{region.x1 * 100:.0f}% 下{region.y1 * 100:.0f}%"
                )
        if surface is self.workbench.view:
            board = self.boards.current()
            text = f"工作台 · {board.name} {len(board.cards)} 页 ｜ {text}"
        # 数字模式顺手把"右键能改起始值/步长"挂在状态栏的模式格子上（悬停可见），
        # 不塞进正文：正文是「数字 · 下一个 N」，测试和眼睛都按这个对齐。
        tip = "数字模式：对着图上空白处点右键，可改起始值与步长" if mode is ToolMode.NUMBER else ""
        self.status.set_mode(text, tip)

    # ------------------------------------------------------------------ 识别区域

    def _start_region_pick(self, which: str) -> None:
        """准备框区域：这次框的是哪一块先记下，再切到画布上的框选模式。"""
        self._region_target = which
        if self.act_workbench.isChecked():
            # 工作台上框的区域没法换算回单页比例，切回单页视图
            self.act_workbench.setChecked(False)
        self.viewer.set_mode(ToolMode.REGION)
        self._refresh_mode_hint()
        hints = {
            "scan": "在页面上拖一个矩形，框住管线号所在的位置",
            "annot": "在页面上拖一个矩形，框住要把匹配结果写上去的位置",
            "magnifier": "在页面上拖一个矩形，框住这个放大窗要放大的那块地方",
            "material": "在页面上拖一个矩形，框住图纸右上角的材料表",
            "page_number": "在页面上拖一个矩形，框住页码要放的位置（所有页共用）",
        }
        self._show_message(hints.get(which, "在页面上拖一个矩形"))

    def _on_region_picked(self, page: int, x0: float, y0: float, x1: float, y1: float) -> None:
        """用户框完一块：换算成页面比例存起来（第 10 章 / 数据匹配标注）。"""
        size = self.viewer.page_size(page)
        if size is None:
            return
        which = self._region_target
        annot = which == "annot"
        magnifier = which == "magnifier"
        material = which == "material"
        page_number = which == "page_number"
        label = (
            "标注位置"
            if annot
            else (
                "放大区域"
                if magnifier
                else (
                    "材料表区域"
                    if material
                    else ("页码位置" if page_number else "识别区域")
                )
            )
        )
        region = Region.from_pdf(x0, y0, x1, y1, size[0], size[1], label)
        if region.is_empty:
            self._show_message("框选的区域太小了，再拖大一点")
            return
        # 区域属于项目设置，第一次框的时候项目可能还没建（用户只框不标）
        if not self.project.is_open:
            self.project.ensure_project()
        if magnifier:
            window = self._magnifier_by_id(self._magnifier_pick_id) or self._active_magnifier()
            if window is None:
                window = self.magnifiers.create(region=region)
            window.region = region
            window.reset_content_zoom()
            self.magnifiers.active = window
            self._apply_magnifier(window, page)
            window.set_wanted_visible(True)
            self._set_act_magnifier_checked(True)
            self.magnifiers.save()
            self.viewer.set_mode(ToolMode.BROWSE)
            self._refresh_mode_hint()
            self._show_message(
                "放大区域已记下：每个放大窗各看一块，右键可再新建 / 置顶（Ctrl+Shift+L 开关）"
            )
            return
        if material:
            self.material_region = region
            self.viewer.set_material_region(region)
            self.project.set_setting("material_region", region.to_dict())
            self.viewer.set_mode(ToolMode.BROWSE)
            self._refresh_mode_hint()
            self._show_message(
                "材料表区域已记下：左 {:.1%} · 上 {:.1%} · 右 {:.1%} · 下 {:.1%}"
                "（每页按同样比例取这块地方）".format(
                    region.x0, region.y0, region.x1, region.y1
                )
            )
            return
        if page_number:
            self.page_number_region = region
            self.project.set_setting("page_number_region", region.to_dict())
            if self.page_numbers is not None:
                self.page_numbers = PageNumberSpec(
                    region=region,
                    template=self.page_numbers.template,
                    style=self.page_numbers.style,
                    enabled=self.page_numbers.enabled,
                )
                self.viewer.set_page_number(self.page_numbers)
                self.project.set_setting("page_number", self.page_numbers.to_dict())
            if self._page_dialog is not None:
                self._page_dialog.set_region(region)
                self._page_dialog.show()
                self._page_dialog.raise_()
                self._page_dialog.activateWindow()
            self.viewer.set_mode(ToolMode.BROWSE)
            self._refresh_mode_hint()
            self._show_message(
                "页码位置已记下：左 {:.1%} · 上 {:.1%} · 右 {:.1%} · 下 {:.1%}"
                "（每页按同样比例写）".format(region.x0, region.y0, region.x1, region.y1)
            )
            return
        if annot:
            self.annot_region = region
            self.viewer.set_annot_region(region)
            self.project.set_setting("annot_region", region.to_dict())
        else:
            self.scan_region = region
            self.viewer.set_scan_region(region)
            self.project.set_setting("scan_region", region.to_dict())
        self.viewer.set_mode(ToolMode.BROWSE)
        self._refresh_mode_hint()
        if self._match_dialog is not None:
            self._match_dialog.refresh_regions()
        self._show_message(
            "{}已记下：左 {:.1%} · 上 {:.1%} · 右 {:.1%} · 下 {:.1%}"
            "（每页按同样比例取这块地方，已存进 .qpdf）".format(
                "标注位置" if annot else "识别区域",
                region.x0,
                region.y0,
                region.x1,
                region.y1,
            )
        )

    def _load_scan_region(self) -> None:
        """打开项目时把上次框的区域恢复出来。"""
        payload = self.project.get_setting("scan_region") if self.project.is_open else None
        region = Region.from_dict(payload) if payload else None
        self.scan_region = region
        self.viewer.set_scan_region(region)
        self._load_annot_region()
        self._load_material_region()
        self._load_page_number()
        self._load_magnifier_region()
        self._refresh_mode_hint()

    def _load_annot_region(self) -> None:
        payload = self.project.get_setting("annot_region") if self.project.is_open else None
        region = Region.from_dict(payload) if payload else None
        self.annot_region = region
        self.viewer.set_annot_region(region)

    def _load_material_region(self) -> None:
        payload = (
            self.project.get_setting("material_region") if self.project.is_open else None
        )
        region = Region.from_dict(payload) if payload else None
        self.material_region = region
        self.viewer.set_material_region(region)

    def _load_page_number(self) -> None:
        """恢复页码位置和格式；页码只是项目设置，不进标注库。"""
        if not self.project.is_open:
            self.page_number_region = None
            self.page_numbers = None
            self.viewer.set_page_number(None)
            return
        region_payload = self.project.get_setting("page_number_region")
        region = Region.from_dict(region_payload) if region_payload else None
        self.page_number_region = region
        payload = self.project.get_setting("page_number")
        spec = PageNumberSpec.from_dict(payload) if payload else None
        if spec is not None and spec.region is None:
            spec = None
        if spec is not None and region is not None:
            # 位置以单独的 page_number_region 为准，避免两处设置不一致
            spec = PageNumberSpec(region, spec.template, spec.style, spec.enabled)
        self.page_numbers = spec
        self.viewer.set_page_number(spec)

    # -------------------------------------------------------- 数据匹配标注

    def open_match_dialog(self) -> None:
        """数据匹配标注（第 1~8 步）：CSV / Excel → 识别 → 匹配 → 自动标注。"""
        if self._doc is None:
            return
        if self._match_dialog is not None and self._match_dialog_owner is self._active:
            self._match_dialog.show()
            self._match_dialog.raise_()
            self._match_dialog.activateWindow()
            return
        if self._match_dialog is not None:
            # 这个对话框绑的是别的标签的 PDF + 项目，关掉它再开新的
            self._match_dialog.close()
            self._match_dialog = None
            self._match_dialog_owner = None
        if not self.project.is_open:
            self.project.ensure_project()
        sizes = self.viewer.page_sizes
        if not sizes:
            sizes = [
                self._doc.page_size(index) for index in range(self._doc.page_count)
            ]
        dialog = MatchDialog(
            self._doc.path, self._doc.page_count, self.project, self.viewer, sizes, self
        )
        dialog.region_requested.connect(self._on_match_region_requested)
        dialog.finished.connect(self._on_match_dialog_finished)
        self._match_dialog = dialog
        self._match_dialog_owner = self._active
        dialog.show()

    def _on_match_region_requested(self, which: str) -> None:
        self._start_region_pick("annot" if which == "annot" else "scan")

    def _on_match_dialog_finished(self, _result: int = 0) -> None:
        self._match_dialog = None
        self._match_dialog_owner = None
        # 对话框写进项目的识别结果要让左侧目录跟着更新
        self._load_pipe_index()

    def _add_page_to_board(self, page: int) -> None:
        if self._doc is None:
            return
        self.workbench.add_page(page)
        if not self.act_workbench.isChecked():
            self.act_workbench.setChecked(True)

    # -------------------------------------------------------------- 管线号目录
    def show_pipe_tree(self) -> None:
        """左侧切到「管线号」页，并保证缩略图栏是可见的。"""
        if not self.act_thumbs.isChecked():
            self.act_thumbs.setChecked(True)
        self._left_tabs.setCurrentWidget(self.pipe_tree)
        self._load_pipe_index()

    # ------------------------------------------------------------------ 目录
    def show_toc(self) -> None:
        """左侧切到「目录」页（PDF 自带 outline，点着跳页）。"""
        if not self.act_thumbs.isChecked():
            self.act_thumbs.setChecked(True)
        self._left_tabs.setCurrentWidget(self.toc)
        if self._doc is not None and self.toc.entry_count() == 0:
            self._load_toc()

    def _load_toc(self) -> None:
        """读 PDF 自带的目录：读不到就显示一句说明，不留空面板。

        识别出来的管线号也并进来（挂在最后一条「识别到的管线号」下面），
        这样这个树既是图纸自己的目录，也能当识别结果的跳转清单用。
        """
        if self._doc is None:
            self._toc_nodes = ()
            self.toc.clear()
            return
        try:
            entries = self._doc.outline()
        except Exception:  # noqa: BLE001
            entries = []
        nodes = list(tocmodel.read_toc(entries))
        nodes.extend(self._ocr_toc_nodes())
        self._toc_nodes = tuple(nodes)
        self.toc.set_outline(self._toc_nodes, self._doc.page_count)

    def _ocr_toc_nodes(self) -> list:
        """把识别出来的管线号（scan_results）做成目录的一支。"""
        if self.project is None or not self.project.is_open:
            return []
        try:
            rows = self.project.load_scan_rows()
        except Exception:  # noqa: BLE001
            return []
        kids = []
        for row in sorted(rows, key=lambda r: int(r.get("page") or 0)):
            tag = str(row.get("tag") or "").strip()
            if not tag:
                continue
            kids.append(tocmodel.TocNode(tag, page=int(row.get("page") or 0), level=2))
        if not kids:
            return []
        return [tocmodel.TocNode(f"识别到的管线号（{len(kids)}）", page=-1, level=1, children=tuple(kids))]

    # ---------------------------------------------------------------- 放大窗
    def _active_magnifier(self):
        active = self.magnifiers.active
        if active is not None and active in self.magnifiers.windows:
            return active
        return self.magnifiers.windows[0] if self.magnifiers.windows else None

    def _magnifier_by_id(self, window_id):
        for window in self.magnifiers.windows:
            if window.win_id == window_id:
                return window
        return None

    def _set_act_magnifier_checked(self, checked: bool) -> None:
        self.act_magnifier.blockSignals(True)
        self.act_magnifier.setChecked(bool(checked))
        self.act_magnifier.blockSignals(False)

    def _on_magnifier_toggled(self, checked: bool) -> None:
        if checked and self._doc is None:
            self._set_act_magnifier_checked(False)
            self._show_message("先打开一份 PDF，再开放大窗")
            return
        if not checked:
            self.magnifiers.set_all_visible(False)
            self._set_act_magnifier_checked(False)
            return
        if not self.magnifiers.windows:
            # 第一次开：建一个，让用户去框区域（没区域就先不弹空窗）
            if self._new_magnifier(None) is None:
                self._set_act_magnifier_checked(False)
            return
        # 已经有窗口：先按「想看见」显示（最小化的也收回来），再抬到最前
        self.magnifiers.set_all_visible(True)
        self._refresh_magnifier(self.viewer.current_page)
        self.magnifiers.set_snap_targets(self._magnifier_snap_targets())
        self.magnifiers.raise_all()
        if not self.magnifiers.any_visible():
            # 一个都显示不出来：多半是还没框区域（没区域不弹空窗）。
            # 这时直接进框区域，别让菜单变成按了没反应的死键。
            window = self._active_magnifier()
            if window is None:
                window = self.magnifiers.create()
                self.magnifiers.active = window
            self._set_act_magnifier_checked(True)
            self._pick_magnifier_region(window)
            return
        self._set_act_magnifier_checked(True)
        self._show_message(
            "放大窗已打开：右键可新建/置顶/锁定；Ctrl+滚轮缩放内容、Ctrl+拖动移内容，侧键翻页"
        )

    def _new_magnifier(self, source=None):
        """再开一个放大窗：复制当前窗口的区域和倍率，位置阶梯错开。"""
        if self._doc is None:
            self._show_message("先打开一份 PDF，再开放大窗")
            return None
        if not isinstance(source, MagnifierWindow):
            source = self._active_magnifier()
        window = self.magnifiers.create_copy(source)
        if window.region is not None:
            self._apply_magnifier(window, self.viewer.current_page)
            window.set_wanted_visible(True)
        else:
            window.hide()
            self._pick_magnifier_region(window)
        self._set_act_magnifier_checked(True)
        self.magnifiers.set_snap_targets(self._magnifier_snap_targets())
        return window

    def _pick_active_magnifier_region(self) -> None:
        window = self._active_magnifier()
        if window is None:
            window = self.magnifiers.create()
            self.magnifiers.active = window
        self._pick_magnifier_region(window)

    def _pick_magnifier_region(self, window) -> None:
        if not isinstance(window, MagnifierWindow):
            return
        self.magnifiers.active = window
        self._magnifier_pick_id = window.win_id
        self._start_region_pick("magnifier")

    def _apply_magnifier(self, window, page: int) -> None:
        if window is None or self._doc is None:
            return
        size = self.viewer.page_size(int(page))
        if size is None:
            return
        if window.region is not None:
            window.set_region(window.region.to_pdf(size[0], size[1]))
        window.refresh(int(page))

    def _refresh_magnifier(self, page: int) -> None:
        """把每个窗口各自的「页面比例区域」换算成当前页绝对坐标，交给它们去渲。"""
        if self._doc is None:
            self.magnifiers.set_document("")
            return
        self.magnifiers.apply_page(int(page), self.viewer.page_size)

    def _refresh_magnifier_annotations(self, *_args) -> None:
        """把当前文档的标注交给放大窗，让它跟页面图一起缩放/平移。"""
        if not hasattr(self, "magnifiers"):
            return
        if self._doc is None:
            self.magnifiers.set_annotations(())
            return
        self.magnifiers.set_annotations(
            self.store.all(),
            self.sequence.text_for,
            self.act_show_all.isChecked(),
        )

    def _step_page_from_magnifier(self, step: int) -> None:
        """放大窗上按侧键：翻主画面的页（靠后下一页 / 靠前上一页），主窗口不激活。"""
        if self._doc is None:
            return
        if int(step) < 0:
            self.viewer.prev_page()
        else:
            self.viewer.next_page()

    def _magnifier_snap_targets(self) -> list:
        """能贴的地方：属性栏、左侧栏、画布，取它们的全局矩形。

        只挡"控件已经被销毁"这一种异常（PySide6 会抛 RuntimeError）；
        别写成 except Exception，那样连 QRect 没导入这类编码错误都会被吞掉。
        """
        targets = []
        for widget in (self.props_dock, self._left_tabs):
            try:
                top_left = widget.mapToGlobal(QPoint(0, 0))
            except (RuntimeError, AttributeError):
                continue
            targets.append(QRect(top_left, widget.size()))
        viewport = self.viewer.viewport()
        if viewport is not None:
            try:
                viewport_origin = viewport.mapToGlobal(QPoint(0, 0))
            except (RuntimeError, AttributeError):
                viewport_origin = None
            if viewport_origin is not None:
                targets.append(QRect(viewport_origin, viewport.size()))
        return targets

    def _on_magnifier_closed(self, window) -> None:
        if not self.magnifiers.any_visible():
            self._set_act_magnifier_checked(False)

    def _on_magnifier_state_changed(self) -> None:
        # 窗口自己关了 / 全部藏起来时，开关跟着回弹；存盘由控制器防抖负责。
        # 控制器在 __init__ 里就 load() 了，那时动作还没建好，所以要 getattr。
        action = getattr(self, "act_magnifier", None)
        if action is not None and action.isChecked() and not self.magnifiers.any_visible():
            self._set_act_magnifier_checked(False)

    def _close_all_magnifiers(self) -> None:
        self.magnifiers.set_all_visible(False)
        self._set_act_magnifier_checked(False)

    def _load_magnifier_region(self) -> None:
        """老项目里只有一份区域/位置：升级后第一次打开时迁移成第一个放大窗。"""
        if self.magnifiers.windows:
            return
        payload = self.project.get_setting("magnifier_region") if self.project.is_open else None
        region = Region.from_dict(payload) if payload else None
        if region is None:
            return
        geometry = self.project.get_setting("magnifier_geometry") if self.project.is_open else None
        window = self.magnifiers.create(region=region, geometry=geometry, visible=True)
        self.magnifiers.active = window
        self.magnifiers.save()

    def _goto_from_pipe_tree(self, page: int) -> None:
        if self._doc is None:
            return
        if self.act_workbench.isChecked():
            self.act_workbench.setChecked(False)
        self.viewer.goto_page(int(page))

    def _load_pipe_index(self) -> None:
        """从项目里读识别结果，顺手算出每页的疑似项给目录标红。"""
        rows: dict[int, dict] = {}
        if self.project.is_open:
            rows = {int(k): dict(v) for k, v in self.project.load_scan_rows().items()}
        for row in rows.values():
            row["suspect"] = qc.find_suspects(row.get("tag") or "", self._ocr_rules())
        self.pipe_tree.set_rows(rows)

    def _rename_pipe_tag(self, old: str, new: str) -> None:
        """把一个管线号改名，并写回项目文件（PDF 本身不动）。"""
        if not self.project.is_open:
            self._show_message("这个文档还没有项目文件，先标一下或者按 Ctrl+S")
            return
        rows = self.project.load_scan_rows()
        changed = 0
        payload: list[dict] = []
        for page, row in rows.items():
            item = dict(row)
            if (item.get("tag") or "") == old:
                item["tag"] = new
                item["raw"] = item.get("raw") or old
                changed += 1
            item["page"] = int(page)
            payload.append(item)
        if not changed:
            self._show_message(f"没找到「{old}」")
            return
        self.project.save_scan_rows(payload)
        self._load_pipe_index()
        self._show_message(f"「{old}」已改名为「{new}」，涉及 {changed} 页（已存入项目文件）")

    def import_outline_into_tree(self) -> None:
        """把这份 PDF 自带的书签目录读进目录树（已经识别出编号的页不动它）。"""
        if self._doc is None:
            self._show_message("先打开一份 PDF")
            return
        nodes = tocmodel.read_toc(self._doc.outline())
        if not nodes:
            self._show_message("这份 PDF 没有自带目录，没东西可导入")
            return
        if not self.project.is_open:
            self.project.ensure_project()
        rows = self.project.load_scan_rows()
        added = 0
        kept = 0
        for node in tocmodel.flatten(nodes):
            if node.page < 0:
                continue
            row = dict(rows.get(node.page) or {})
            if (row.get("tag") or "").strip():
                kept += 1
                continue
            row.setdefault("page", node.page)
            row["raw"] = node.title
            row["tag"] = node.title.strip()[:80]
            row["source"] = "outline"
            row["fixed"] = 0
            row["confirmed"] = 1
            row["note"] = "来自 PDF 目录"
            rows[node.page] = row
            added += 1
        if not added:
            self._show_message(f"目录里的 {kept} 页都已经有编号了，没有改")
            return
        self.project.save_scan_rows([dict(row, page=int(page)) for page, row in rows.items()])
        self._load_pipe_index()
        self._left_tabs.setCurrentWidget(self.pipe_tree)
        self._show_message(
            f"从 PDF 目录导入了 {added} 条" + (f"，{kept} 页已有编号没覆盖" if kept else "")
        )

    def merge_toc_with_bookmarks(self) -> None:
        """OCR 识别结果 ↔ PDF 原书签：以书签为底比对、勾选，最后整棵树换成这份结果。

        注意这里不是"往目录树里补几条"——目录树此刻装的是上一轮 OCR 的产物，
        而这次的结果是一份**以 PDF 自带书签为底**的新目录，所以要先清再写，
        否则没勾的旧 OCR 行会赖在树里。
        """
        if self._doc is None:
            self._show_message("先打开一份 PDF")
            return
        if not self.project.is_open:
            self.project.ensure_project()
        page_count = int(getattr(self._doc, "page_count", 0) or 0)
        entries = self._doc.outline() or []
        ocr_rows = self.project.load_scan_rows() or {}
        if not entries and not ocr_rows:
            self._show_message("这份图纸既没有自带书签，也还没识别过，先批量识别一次")
            return
        rows = build_merge_rows(ocr_rows, entries, page_count)
        dialog = TocMergeDialog(self, rows=rows, page_count=page_count)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        payload = dialog.result_rows()
        # 整棵树换成这份合并结果（书签为底 + 勾选的识别结果）
        self.project.clear_scan_rows()
        if payload:
            self.project.save_scan_rows(payload)
        self._load_pipe_index()
        self._left_tabs.setCurrentWidget(self.pipe_tree)
        from_ocr = sum(1 for item in payload if item.get("source") == "ocr")
        from_outline = len(payload) - from_ocr
        if not payload:
            self._show_message("目录树已清空：没有任何书签，也没勾识别结果")
        else:
            self._show_message(
                "目录树已换成 %d 条（书签 %d 条、识别结果 %d 条）"
                % (len(payload), from_outline, from_ocr)
            )

    def clear_directory_tree(self) -> None:
        """清空目录树里的识别结果（PDF、标注、书签设置都不动）。"""
        if not self.project.is_open:
            self._show_message("还没有项目文件，目录树本来就是空的")
            return
        answer = QMessageBox.question(
            self,
            "清空目录树",
            "清空这份图纸的识别结果和导入的目录？\n\n标注不会被删，PDF 也不会动；"
            "清完可以重新识别或重新导入。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.project.clear_scan_rows()
        self.pipe_tree.clear()
        self._show_message("目录树已清空")

    def _ocr_rules(self) -> dict:
        payload = self.project.get_setting("ocr_rules") if self.project.is_open else None
        return qc.load_rules(payload)

    # ------------------------------------------------------------------ 标注管理

    def _toggle_locked(self) -> None:
        count = self.viewer.toggle_locked_selected()
        if count:
            self.status.showMessage(f"已切换 {count} 个标注的锁定状态", 2500)

    def _toggle_hidden(self) -> None:
        count = self.viewer.toggle_hidden_selected()
        if count:
            self.status.showMessage(f"已切换 {count} 个标注的显示状态", 2500)
            self.act_unhide_all.setEnabled(self.store.hidden_count > 0)

    def _unhide_all(self) -> None:
        """把被隐藏的标注全部放出来（一条命令，可撤销）。"""
        hidden = [ann for ann in self.store.all() if ann.hidden]
        if not hidden:
            return
        self.undo_stack.beginMacro(f"显示 {len(hidden)} 个隐藏的标注")
        try:
            for ann in hidden:
                self.undo_stack.push(
                    ModifyAnnotationCommand(self.store, ann, ann.with_hidden(False), "显示标注")
                )
        finally:
            self.undo_stack.endMacro()
        self.act_unhide_all.setEnabled(self.store.hidden_count > 0)
        self.status.showMessage(f"已显示 {len(hidden)} 个隐藏的标注", 2500)

    def _toggle_eye_care(self, on: bool) -> None:
        """护眼模式：只换页面区底色，白纸和图纸颜色一律不动。"""
        theme.set_eye_care(bool(on))
        self._settings.setValue("eye_care", bool(on))
        brush = QColor(theme.canvas_color())
        for view in (self.viewer, self.workbench.view):
            view.setBackgroundBrush(brush)
            view.viewport().update()
        self.status.showMessage("护眼模式已开启" if on else "护眼模式已关闭", 2000)

    # ------------------------------------------------------------------ 编号

    def restart_numbering(self) -> None:
        """第 5.4 节「重新开始编号」：新开一组从 1 开始，已有数字不动。"""
        if self._doc is None:
            return
        seq = self.sequence.restart(1)
        self.viewer.set_mode(ToolMode.NUMBER)
        self._refresh_mode_hint()
        self.status.showMessage(f"已开始新的一组编号：{seq.label}，从 1 重新开始", 3000)

    def number_settings_dialog(self) -> None:
        if self._doc is None:
            return
        current = self.sequence.current
        dialog = NumberSettingsDialog(current.start, current.step, self)
        if not dialog.exec():
            return
        start, step = dialog.values()
        seq = self.sequence.configure(start, step)
        self.viewer.set_mode(ToolMode.NUMBER)
        self._refresh_mode_hint()
        self.status.showMessage(
            f"{seq.label}：{start} 起，步长 {step}，下一个是 {self.sequence.next_text(seq.group)}",
            3500,
        )

    def _show_message(self, text: str) -> None:
        self.status.showMessage(text, 4000)

    # ------------------------------------------------------------------ 覆盖 / 插队

    def set_detect_radius(self, radius: float) -> None:
        self.viewer.set_detect_radius(radius)
        for act in self._radius_actions.values():
            act.setChecked(abs(float(act.data()) - self.viewer.detect_radius) < 0.01)
        self.status.showMessage(f"覆盖检测半径：{self.viewer.detect_radius:g} pt", 2500)

    def custom_detect_radius(self) -> None:
        value, ok = QInputDialog.getDouble(
            self,
            "覆盖检测半径",
            "点击位置多少 pt 以内算命中（1 – 500）：",
            self.viewer.detect_radius,
            1.0,
            500.0,
            1,
        )
        if ok:
            self.set_detect_radius(value)

    def force_refresh_page(self) -> None:
        """工具栏/视图菜单的「强制刷新」：丢缓存重渲当前页，顺带刷新放大窗。

        用户遇到画面空白、显示不对（比如别处写坏了缓存、缩放没跟上来）时，
        不需要知道原因，点一下就有图。
        """
        if self._doc is None:
            self._show_message("还没有打开图纸")
            return
        self.viewer.force_refresh()
        page = self.viewer.current_page
        try:
            self.magnifiers.refresh_visible(page)
        except Exception:  # noqa: BLE001 - 放大窗刷新失败不影响主画面
            pass
        self.thumbs.reload_current()
        self._show_message(f"已强制重新渲染第 {page + 1} 页")

    def _on_insert_requested(
        self, page: int, x: float, y: float, at_index: int, group: str
    ) -> None:
        """插队前先让用户确认影响范围（第 6.2.2 节），并给出操作预览。"""
        if self._doc is None:
            return
        sequence = self.sequence
        seq = sequence.sequence(group)
        value_text = seq.text_at(at_index)

        def preview(scope: str, page_from: int, page_to: int) -> tuple[int, str]:
            pairs = sequence.plan_insert(
                at_index, scope, page=page, page_from=page_from, page_to=page_to, group=group
            )
            if not pairs:
                return 0, ""
            samples = [
                f"{seq.text_at(old.seq_index)} → {seq.text_at(new.seq_index)}"
                for old, new in pairs[:4]
            ]
            detail = "，".join(samples)
            if len(pairs) > len(samples):
                detail += f" …（共 {len(pairs)} 个）"
            return len(pairs), detail

        dialog = InsertNumberDialog(
            at_index, value_text, page, self._doc.page_count, preview, self
        )
        if not dialog.exec():
            return
        scope, page_from, page_to = dialog.scope()
        count = self.viewer.apply_insert(
            page, x, y, at_index, scope, page_from, page_to, group=group
        )
        if count:
            self.status.showMessage(
                f"已在第 {value_text} 号位置插入新编号，{count - 1} 个数字后移一格（可 Ctrl+Z 撤销）",
                4000,
            )

    def _on_selection_changed(self, anns: list) -> None:
        has_sel = bool(anns)
        # 点中标注时说明用户要改标注，不再让属性面板停留在页码模式
        if has_sel and self.props.page_mode:
            self.props.set_page_mode(False)
        self.act_delete.setEnabled(has_sel)
        self.act_copy.setEnabled(has_sel)
        self.act_edit_text.setEnabled(has_sel)
        self.act_lock.setEnabled(has_sel)
        self.act_hide.setEnabled(has_sel)
        self.act_paste.setEnabled(bool(self.viewer._clipboard))
        self._refresh_style_from_selection()

    def _refresh_style_from_selection(self) -> None:
        if self.props.page_mode and self.page_numbers is not None:
            self.props.set_style(self.page_numbers.style, 0)
            return
        anns = self.viewer.selected_annotations()
        style = anns[0].style if anns else self.viewer.default_style
        self.props.set_style(style, len(anns))

    def _on_style_changed(self, style: AnnotationStyle) -> None:
        anns = self.viewer.selected_annotations()
        if anns:
            self.viewer.apply_style_to_selection(style)
        else:
            self.viewer.set_default_style(style)

    def _delete_selected(self) -> None:
        count = self.viewer.delete_selected()
        if count:
            self.status.showMessage(f"已删除 {count} 个标注", 2500)

    def _copy_selected(self) -> None:
        count = self.viewer.copy_selected()
        if count:
            self.act_paste.setEnabled(True)
            self.status.showMessage(f"已复制 {count} 个标注", 2000)

    def _paste_clipboard(self) -> None:
        count = self.viewer.paste_clipboard()
        if count:
            self.status.showMessage(f"已粘贴 {count} 个标注", 2000)

    def _refresh_page_label(self) -> None:
        if self._doc is None:
            self._page_label.setText("  —  ")
        else:
            self._page_label.setText(
                f"  {self.viewer.current_page + 1} / {self._doc.page_count}  "
            )

    # -- 双击页码直接跳 -----------------------------------------------------

    def _edit_page_number(self) -> None:
        """双击工具栏上的页码 → 原地长出一个输入框。"""
        if self._doc is None:
            return
        label = self._page_label
        top_left = label.mapTo(self, QPoint(0, 0))
        self._page_editor.setGeometry(
            top_left.x(), top_left.y(), max(label.width(), 84), label.height()
        )
        self._page_editor.setText(str(self.viewer.current_page + 1))
        self._page_editor.selectAll()
        self._page_editor.show()
        self._page_editor.raise_()
        self._page_editor.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def _commit_page_number(self) -> None:
        if not self._page_editor.isVisible():
            return
        text = self._page_editor.text().strip()
        self._page_editor.hide()
        if self._doc is None or not text:
            return
        try:
            page_no = int(text)
        except ValueError:
            self._show_message("页码要输入数字")
            return
        total = self._doc.page_count
        if page_no < 1 or page_no > total:
            self._show_message(f"页码要在 1 到 {total} 之间")
            return
        self.viewer.goto_page(page_no - 1)

    def _cancel_page_edit(self) -> None:
        self._page_editor.hide()

    def _refresh_cache_stat(self) -> None:
        self.status.set_cache_bytes(self.render_manager.cache_bytes)

    def show_about(self) -> None:
        QMessageBox.about(
            self,
            "关于 QPDF",
            "<b>QPDF — 雀雀 PDF</b><br>"
            "大型 PDF 工程图纸轻量化标注工具<br><br>"
            "当前版本：V1.1 Phase 3（连续数字标注）<br>"
            "完全离线运行，不修改原始 PDF。",
        )

    # ------------------------------------------------------------------ 最近文件

    def _recent_files(self) -> list[str]:
        value = self._settings.value("recent_files", [], type=list)
        return [p for p in value if isinstance(p, str) and os.path.isfile(p)]

    def _push_recent(self, path: str) -> None:
        files = [p for p in self._recent_files() if p != path]
        files.insert(0, path)
        self._settings.setValue("recent_files", files[:MAX_RECENT])
        self._rebuild_recent_menu()

    def _rebuild_recent_menu(self) -> None:
        self._recent_menu.clear()
        files = self._recent_files()
        if not files:
            action = self._recent_menu.addAction("（暂无）")
            action.setEnabled(False)
            return
        for path in files:
            action = self._recent_menu.addAction(os.path.basename(path))
            action.setToolTip(path)
            action.triggered.connect(lambda _=False, p=path: self.open_pdf(p))

    # ------------------------------------------------------------------ 拖放

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path.lower().endswith(".pdf"):
                self.open_pdf(path)
                break

    # ------------------------------------------------------------------ 关闭

    def _ask_save_on_close(self, ds: DocumentSession | None = None) -> str:
        """退出/关标签前问一句；返回 ``"save"`` / ``"discard"`` / ``"cancel"``。"""
        name = ""
        if ds is not None:
            name = f"「{ds.title}」"
        choice = QMessageBox.question(
            self,
            "还有修改没保存",
            f"{name}这次的修改还没保存。\n\n"
            "「保存」写进项目文件；「不保存」还原成上次手动保存的样子。",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if choice == QMessageBox.StandardButton.Save:
            return "save"
        if choice == QMessageBox.StandardButton.Discard:
            return "discard"
        return "cancel"

    def _confirm_close_after_save_failure(self, ds: DocumentSession, exc: Exception) -> bool:
        """保存没成功时要问一句：真关掉，这次改动就没了。返回 True = 仍然关闭。"""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("保存失败")
        box.setText(f"「{ds.title}」保存失败：{exc}")
        box.setInformativeText(
            "现在关掉的话，这次的修改会全部丢掉。\n"
            "可以先换个「项目库」位置（设置 → 项目库）或腾出磁盘空间，再按 Ctrl+S 重试。"
        )
        keep = box.addButton("先不关，我去处理", QMessageBox.ButtonRole.RejectRole)
        drop = box.addButton("仍然关闭（放弃修改）", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(keep)
        box.exec()
        return box.clickedButton() is drop

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._match_dialog is not None:
            self._match_dialog.close()
            self._match_dialog = None
        # 每个有未保存改动的标签都问一次；点「取消」就整个退出中止
        for ds in list(self._sessions):
            if not ds.project.is_dirty:
                continue
            answer = self._ask_save_on_close(ds)
            if answer == "cancel":
                event.ignore()
                return
            try:
                if answer == "discard":
                    ds.project.discard_unsaved()
                else:
                    ds.project.save()
            except ProjectError as exc:
                # 以前这里是 `except Exception: pass`——保存失败照样退，改动无声消失。
                if not self._confirm_close_after_save_failure(ds, exc):
                    event.ignore()
                    return
        self.export_manager.shutdown()  # 正在导出就取消并等线程退出
        self.extract_manager.shutdown()
        self.magnifiers.shutdown()
        self.thumbs.shutdown()
        for ds in list(self._sessions):
            ds.dispose()
        self._sessions.clear()
        self._active = None
        self._doc = None
        super().closeEvent(event)
