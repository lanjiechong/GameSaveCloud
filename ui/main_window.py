# -*- coding: utf-8 -*-
"""
主窗口。

布局：
    顶部工具栏 —— 立即备份 / 同步（备份）全部 / 添加游戏 / 设置 / 刷新
    左侧        —— 游戏列表（图标 + 两行文字 + 状态点，自绘委托）
    右侧        —— 状态卡片 + 历史版本表 + 操作按钮
    底部        —— 日志

设计原则：
- 所有 IO 走线程池，主线程只更新控件
- 危险操作（回滚、覆盖）必须二次确认
- 冲突出现时中断自动同步，弹窗交人决策，绝不静默二选一
- **本地备份为主，云备份为可选项**：没配云端时所有远端操作一律不跑，
  界面文案随之从「同步」切换为「备份」
"""
import os
import sys
import time

from PySide6.QtCore import Qt, QTimer, QSize
from PySide6.QtGui import QColor, QAction, QIcon
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QTableWidget, QTableWidgetItem, QHeaderView,
    QGroupBox, QGridLayout, QPlainTextEdit, QSplitter, QMessageBox,
    QAbstractItemView, QProgressBar, QMenu, QSystemTrayIcon, QApplication,
    QFrame, QDialog, QDialogButtonBox,
)

from .theme import PALETTE, STYLESHEET, state_color, state_bg, state_label
from .dialogs import (ConfirmDialog, ConflictDialog, AddGameDialog,
                      HistoryDialog, DiffDialog, SettingsDialog, AboutDialog,
                      _fmt, _human)
from .workers import run_async
from .game_list import GameItemDelegate, ROLE_GAME, ROLE_STATUS, ROLE_CLOUD
from .logos import LogoService
from gamesave.config import Config
from gamesave.vault import Vault
from gamesave.remote import LocalFolderBackend, WebDAVBackend, RemoteError
from gamesave.sync import SyncEngine
from gamesave import scanner
from gamesave.models import (SYNC_SYNCED, SYNC_LOCAL_AHEAD, SYNC_REMOTE_AHEAD,
                               SYNC_CONFLICT, SYNC_UNKNOWN, SYNC_ERROR, GameStatus)


def _asset(name: str) -> str:
    """内置资源路径。开发时在 ui/assets/，PyInstaller 打包后在 _MEIPASS。"""
    if getattr(sys, "_MEIPASS", None):
        return os.path.join(sys._MEIPASS, "ui", "assets", name)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", name)


def _app_icon() -> QIcon:
    """应用图标：优先多尺寸 ICO（任务栏小图标更清晰），退回 PNG。"""
    ic = QIcon()
    for n in ("app_logo.ico", "app_logo.png"):
        p = _asset(n)
        if os.path.isfile(p):
            ic.addFile(p)
    return ic


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        from version import APP_NAME, APP_TAGLINE, VERSION
        self.setWindowTitle("%s v%s —— %s" % (APP_NAME, VERSION, APP_TAGLINE))
        self.resize(1120, 760)
        self.setStyleSheet(STYLESHEET)
        self.setWindowIcon(_app_icon())

        self.cfg = Config()
        self.vault = Vault(self.cfg.vault_root)
        self.remote = self._build_remote()
        self.engine = SyncEngine(self.vault, self.remote, log=self._log,
                                 cloud_on=self.cloud_on)
        self.statuses = {}          # game_id -> GameStatus
        self.busy = False
        self.last_plan = None
        # 图标服务：磁盘缓存 → 内置预设 → 联网检索 → 占位兜底
        self.logos = LogoService(cache_dir=os.path.join(
            self.cfg.base, "logocache"))
        self.logos.logo_ready.connect(self._on_logo_ready)

        self._build_ui()
        self._build_tray()
        self._build_timer()
        self._update_mode_ui()

        QTimer.singleShot(50, self.refresh_all)

    # ==========================================================
    # 基础设施
    # ==========================================================
    @property
    def cloud_on(self) -> bool:
        """云端同步是否启用。未配置云端时一切远端操作都不跑。"""
        return self.cfg.cloud_enabled

    def _build_remote(self):
        if not self.cloud_on:
            # 本地备份模式：干脆不造后端对象。
            # 少建一个目录，也少一条「忘了判断就去连云端」的代码路径。
            return None
        r = self.cfg.remote_conf
        if r.get("type") == "webdav" and r.get("url"):
            return WebDAVBackend(r["url"], r.get("user", ""), r.get("password", ""))
        return LocalFolderBackend(r.get("path") or os.path.join(
            self.cfg.base, "GameSaveCloud"))

    def _rebuild_backend(self):
        """设置里改了云端配置后，重建后端与引擎。"""
        self.remote = self._build_remote()
        self.engine = SyncEngine(self.vault, self.remote, log=self._log,
                                 cloud_on=self.cloud_on)
        self._update_mode_ui()
        self._log("云端配置已变更：%s" % self.cfg.cloud_label())

    def _log(self, msg):
        ts = time.strftime("%H:%M:%S")
        if hasattr(self, "log_view"):
            self.log_view.appendPlainText("[%s] %s" % (ts, msg))

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        # ---- 工具栏 ----
        bar = QHBoxLayout()
        self.b_backup = QPushButton("立即备份")
        self.b_backup.setObjectName("Primary")
        self.b_backup.clicked.connect(self.backup_current)
        self.b_sync = QPushButton("同步全部")
        self.b_sync.clicked.connect(self.sync_all)
        self.b_add = QPushButton("添加游戏")
        self.b_add.clicked.connect(self.add_game)
        self.b_settings = QPushButton("设置")
        self.b_settings.clicked.connect(self.open_settings)
        self.b_refresh = QPushButton("刷新")
        self.b_refresh.clicked.connect(self.refresh_all)
        self.b_stop = QPushButton("取消")
        self.b_stop.setEnabled(False)
        self.b_stop.clicked.connect(self.cancel_current)
        self.b_about = QPushButton("关于")
        self.b_about.clicked.connect(self.open_about)
        for b in (self.b_backup, self.b_sync, self.b_add, self.b_settings,
                  self.b_refresh, self.b_stop, self.b_about):
            bar.addWidget(b)
        bar.addStretch(1)
        self.vault_info = QLabel()
        self.vault_info.setObjectName("Muted")
        bar.addWidget(self.vault_info)
        root.addLayout(bar)

        # ---- 进度条 ----
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(4)
        self.progress.hide()
        root.addWidget(self.progress)

        # ---- 主体：左右分栏 ----
        split = QSplitter(Qt.Horizontal)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(6)
        head = QHBoxLayout()
        head.addWidget(QLabel("游戏"))
        head.addStretch(1)
        self.mode_chip = QLabel()
        self.mode_chip.setObjectName("ModeChip")
        head.addWidget(self.mode_chip)
        ll.addLayout(head)
        self.game_list = QListWidget()
        self.game_list.setUniformItemSizes(True)   # 行高一致，滚动更快
        self.game_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # 自绘委托：图标 + 文本两栏、固定间距、省略号 + 悬浮全称
        self.game_delegate = GameItemDelegate(self.logos, self.game_list)
        self.game_list.setItemDelegate(self.game_delegate)
        self.game_list.currentRowChanged.connect(self.on_game_selected)
        ll.addWidget(self.game_list)
        split.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)

        # 状态卡片
        self.card = QGroupBox()
        cg = QGridLayout(self.card)
        cg.setContentsMargins(14, 14, 14, 14)
        cg.setHorizontalSpacing(24)
        cg.setVerticalSpacing(6)
        self.lbl_title = QLabel("选择一个游戏")
        self.lbl_title.setObjectName("H1")
        cg.addWidget(self.lbl_title, 0, 0, 1, 4)
        self.lbl_state = QLabel("—")
        self.lbl_state.setObjectName("H2")
        cg.addWidget(self.lbl_state, 1, 0)
        self.lbl_last = QLabel("最后同步：—")
        cg.addWidget(self.lbl_last, 1, 1)
        self.lbl_size = QLabel("本地 — · 云端 —")
        cg.addWidget(self.lbl_size, 1, 2)
        self.lbl_snaps = QLabel("快照 0 个")
        cg.addWidget(self.lbl_snaps, 1, 3)
        self.lbl_msg = QLabel("")
        self.lbl_msg.setObjectName("Muted")
        self.lbl_msg.setWordWrap(True)
        cg.addWidget(self.lbl_msg, 2, 0, 1, 4)
        rl.addWidget(self.card)

        # 操作按钮
        acts = QHBoxLayout()
        self.b_history = QPushButton("历史版本")
        self.b_history.clicked.connect(self.show_history)
        self.b_files = QPushButton("查看文件")
        self.b_files.clicked.connect(self.show_files)
        self.b_sync_one = QPushButton("同步此游戏")
        self.b_sync_one.setObjectName("Primary")
        self.b_sync_one.clicked.connect(self.sync_current)
        self.b_remove = QPushButton("移除游戏")
        self.b_remove.clicked.connect(self.remove_game)
        for b in (self.b_history, self.b_files, self.b_sync_one, self.b_remove):
            acts.addWidget(b)
        acts.addStretch(1)
        rl.addLayout(acts)

        # 历史版本表
        rl.addWidget(QLabel("历史版本（最新在上）"))
        self.snap_table = QTableWidget(0, 4)
        self.snap_table.setHorizontalHeaderLabels(["时间", "类型", "文件数", "体积"])
        self.snap_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.snap_table.verticalHeader().setVisible(False)
        self.snap_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.snap_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        rl.addWidget(self.snap_table, 1)

        split.addWidget(right)
        split.setStretchFactor(1, 3)
        split.setSizes([300, 800])
        root.addWidget(split, 1)

        # ---- 日志 ----
        root.addWidget(QLabel("日志"))
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setFixedHeight(150)
        root.addWidget(self.log_view)

    def _build_tray(self):
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(_app_icon())
        menu = QMenu()
        menu.addAction("显示主窗口", self.showNormal)
        menu.addAction("立即同步", self.sync_all)
        menu.addAction("关于", self.open_about)
        menu.addSeparator()
        menu.addAction("退出", QApplication.quit)
        self.tray.setContextMenu(menu)
        from version import APP_NAME, VERSION
        self.tray.setToolTip("%s v%s" % (APP_NAME, VERSION))
        self.tray.show()

    def _build_timer(self):
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._on_tick)
        self._apply_schedule()

    def _apply_schedule(self):
        s = self.cfg.schedule
        if s.get("enabled"):
            self.timer.start(max(5, int(s.get("interval_minutes", 60))) * 60 * 1000)
            self._log("定时同步已启用，间隔 %d 分钟" % s.get("interval_minutes", 60))
        else:
            self.timer.stop()

    def _on_tick(self):
        if self.busy:
            return
        self._log("定时同步触发")
        if self.cfg.schedule.get("silent"):
            self.sync_all(silent=True)
        else:
            self.sync_all()

    # ==========================================================
    # 数据加载
    # ==========================================================
    def current_game(self):
        it = self.game_list.currentItem()
        return it.data(Qt.UserRole) if it else None

    def refresh_all(self):
        self._set_busy(True, "正在扫描…")
        games = self.vault.list_games()

        def job(sig):
            out = []
            for g in games:
                out.append(self.engine.status(g))
            return games, out

        def done(res):
            games, sts = res
            self.statuses = {s.game_id: s for s in sts}
            self.game_list.clear()
            for g, s in zip(games, sts):
                it = QListWidgetItem()
                it.setData(ROLE_GAME, g)
                it.setData(ROLE_STATUS, s)
                it.setData(ROLE_CLOUD, self.cloud_on)
                self.game_list.addItem(it)
                # 图标：先给内置预设/占位（立即可见），再异步联网找真图
                self.logos.pixmap(g.name)
                self.logos.request(g.name)
            self._update_vault_info()
            self._update_mode_ui(sts)
            if self.game_list.count():
                self.game_list.setCurrentRow(0)
            self._set_busy(False)
            for s in sts:
                if s.state == SYNC_CONFLICT:
                    self._log("⚠ %s 存在 %s" % (s.game_id, s.message))
            self._log("刷新完成，共 %d 款游戏" % len(games))

        run_async(job, on_result=done,
                  on_error=lambda e: (self._set_busy(False), self._err_bg(e)))

    def _on_logo_ready(self, name):
        """联网图标到手了，把同名条目重绘一遍。

        只重绘受影响的几行，不整表刷新 —— 整表刷新会把选中位置和
        滚动位置也一起重置。
        """
        for i in range(self.game_list.count()):
            it = self.game_list.item(i)
            g = it.data(ROLE_GAME)
            if g is not None and g.name == name:
                self.game_list.update(self.game_list.indexFromItem(it))

    def _update_mode_ui(self, sts=None):
        """按云端开关切换按钮文案与模式标签。

        没配云端时「同步」就是「备份到本地保险库」，
        按钮还写「同步全部」会让人以为数据去了云端。
        """
        on = self.cloud_on
        self.b_sync.setText("同步全部" if on else "备份全部")
        self.b_sync_one.setText("同步此游戏" if on else "备份此游戏")
        if on:
            pending = 0
            for s in (sts if sts is not None else self.statuses.values()):
                if s.state in (SYNC_LOCAL_AHEAD, SYNC_REMOTE_AHEAD, SYNC_CONFLICT):
                    pending += 1
            if pending:
                self.mode_chip.setText("云端待同步 %d 款" % pending)
                self.mode_chip.setStyleSheet(
                    "background:%s; color:%s; border-radius:9px;"
                    "padding:2px 9px; font-size:11px; font-weight:600;"
                    % (PALETTE["local_bg"], PALETTE["local"]))
            else:
                self.mode_chip.setText("云端已启用")
                self.mode_chip.setStyleSheet(
                    "background:%s; color:%s; border-radius:9px;"
                    "padding:2px 9px; font-size:11px; font-weight:600;"
                    % (PALETTE["synced_bg"], PALETTE["synced"]))
        else:
            self.mode_chip.setText("本地备份")
            self.mode_chip.setStyleSheet(
                "background:%s; color:%s; border-radius:9px;"
                "padding:2px 9px; font-size:11px; font-weight:600;"
                % (PALETTE["unknown_bg"], PALETTE["muted"]))

    def _update_vault_info(self):
        st = self.vault.stats()
        self.vault_info.setText("保险库：%d 款游戏 · %d 个快照 · %d 个 chunk · %s"
                                % (st["games"], st["snapshots"], st["chunks"],
                                   _human(st["chunk_bytes"])))

    def on_game_selected(self):
        g = self.current_game()
        if not g:
            return
        self._refresh_detail(g)
        self._load_snapshots(g)

    def _refresh_detail(self, g):
        s = self.statuses.get(g.id) or GameStatus(game_id=g.id)
        on = self.cloud_on
        self.lbl_title.setText(g.name)
        self.lbl_state.setText(state_label(s.state, on))
        self.lbl_state.setStyleSheet(
            "color:%s; font-weight:700; padding:2px 10px; border-radius:10px;"
            "background:%s;" % (state_color(s.state), state_bg(s.state)))
        self.lbl_last.setText("最后同步：%s" % (_fmt(s.last_sync_at) if s.last_sync_at else "从未"))
        # 本地模式下「云端」那一栏装的是最新快照，标签要跟着改
        far = "云端" if on else "最新备份"
        self.lbl_size.setText("本地 %s · %s %s" % (_human(s.local_size), far,
                                                  _human(s.remote_size)))
        self.lbl_snaps.setText("快照 %d 个" % s.snapshot_count)
        self.lbl_msg.setText(s.message or "")
        if s.state == SYNC_CONFLICT:
            self.lbl_msg.setStyleSheet("color:%s; font-weight:600;" % PALETTE["conflict"])
        else:
            self.lbl_msg.setStyleSheet("color:%s;" % PALETTE["muted"])

    def _load_snapshots(self, g):
        try:
            snaps = self.vault.list_snapshots(g.id)
        except Exception as e:
            self._err(e)
            return
        self.snap_table.setRowCount(len(snaps))
        for r, s in enumerate(snaps):
            kind = {"manual": "手动", "auto": "自动", "sync": "同步",
                    "restore": "回滚"}.get(s.kind, s.kind)
            self.snap_table.setItem(r, 0, QTableWidgetItem(_fmt(s.created_at)))
            self.snap_table.setItem(r, 1, QTableWidgetItem(kind))
            self.snap_table.setItem(r, 2, QTableWidgetItem(str(len(s.files))))
            self.snap_table.setItem(r, 3, QTableWidgetItem(_human(s.total_size)))
            self.snap_table.item(r, 0).setData(Qt.UserRole, s)

    # ==========================================================
    # 操作
    # ==========================================================
    def _set_busy(self, on, msg=""):
        self.busy = on
        for b in (self.b_backup, self.b_sync, self.b_add, self.b_refresh,
                  self.b_history, self.b_files, self.b_sync_one, self.b_remove):
            b.setEnabled(not on)
        self.b_stop.setEnabled(on)
        if on:
            self.progress.show()
        else:
            self.progress.hide()
        if msg:
            self._log(msg)

    def cancel_current(self):
        self._cancelled = True
        self._log("已请求取消（当前文件处理完后停止）")

    def _err(self, e):
        """用户主动触发的操作失败 —— 弹模态框。

        只用于「用户按了某个按钮、正在等结果」的路径。此时弹窗是合理的：
        用户确实需要知道刚才那下点击失败了。
        """
        self._log("错误：%s" % e)
        QMessageBox.critical(self, "出错", str(e))

    def _err_bg(self, e):
        """后台任务失败 —— 只记日志，绝不弹模态框。

        后台路径（启动后的首次刷新、定时同步、托盘触发）从工作线程的
        on_error 回来时若弹模态框，会把整个应用挂住：用户没在看窗口，
        对话框却霸占了事件循环，后续刷新全进不来（离屏自测里表现为
        永久卡死，界面上 busy 标志永远不回落）。
        后台失败的正确形态是「日志留痕 + 界面标成错误状态」，
        用户下次看窗口时能从日志和状态点自己发现。
        """
        self._log("后台任务失败：%s" % e)

    # ---------- 立即备份 ----------
    def backup_current(self):
        g = self.current_game()
        if not g:
            QMessageBox.information(self, "未选择", "请先在左侧选择一个游戏。")
            return
        self._set_busy(True, "正在备份 %s …" % g.name)

        def job(sig):
            return self.vault.create_snapshot(g, kind="manual",
                                              note="手动备份",
                                              progress_cb=lambda c, t: sig.progress.emit(
                                                  "备份 %s" % g.name, c, t))

        def done(snap):
            self._log("备份完成：%d 个文件 / %s" % (len(snap.files), _human(snap.total_size)))
            self._prune(g)
            self._set_busy(False)
            self._load_snapshots(g)
            self._update_vault_info()
            tail = ("备份已存入本地保险库。" if not self.cloud_on else
                    "备份已存入本地保险库，点「同步全部」才会传到云端。")
            QMessageBox.information(self, "备份完成",
                                    "%s 已备份 %d 个文件（%s）。\n\n%s"
                                    % (g.name, len(snap.files),
                                       _human(snap.total_size), tail))

        run_async(job, on_result=done, on_progress=self._on_progress,
                  on_error=lambda e: (self._set_busy(False), self._err(e)))

    def _on_progress(self, phase, cur, tot):
        if tot > 0:
            self.progress.setMaximum(tot)
            self.progress.setValue(cur)
            self.progress.setFormat("%s %d/%d" % (phase, cur, tot))
        else:
            self.progress.setMaximum(0)
            self.progress.setFormat(phase)

    def _prune(self, g):
        """按「每款游戏保留快照数」裁剪本地与云端的旧快照。

        配置项此前的状态是「设置了但没有任何代码读它」。定时同步一小时
        一次，不裁剪的话备份目标会无限增长，而 chunk 去重本来就是为了
        避免这件事。同步基准快照受保护，不参与裁剪。
        本地备份模式下只裁本地 —— 没有云端可裁。
        """
        keep = int(self.cfg.safety.get("keep_snapshots", 30) or 0)
        if keep <= 0:
            return
        try:
            n = self.vault.prune_snapshots(g.id, keep)
            nr = 0
            if self.remote is not None:
                ss = self.vault.get_sync_state(g.id) or {}
                nr = self.remote.prune_snapshots(g.id, keep,
                                                 protect={ss.get("base_snapshot_id")})
            if n or nr:
                self._log("已裁剪 %s 的旧快照：本地 %d 份 / 云端 %d 份"
                          "（保留最近 %d 版）" % (g.name, n, nr, keep))
        except Exception as e:
            self._log("裁剪 %s 的快照失败：%s" % (g.name, e))

    # ---------- 同步 ----------
    def sync_current(self):
        g = self.current_game()
        if not g:
            return
        if not self.cloud_on:
            # 本地模式下「同步此游戏」就是给这一款存一份快照
            self.backup_current()
            return
        self._sync_games([g], silent=False)

    def sync_all(self, silent=False):
        games = self.vault.list_games()
        if not games:
            if not silent:
                QMessageBox.information(self, "没有游戏", "请先添加要备份的游戏。")
            return
        if not self.cloud_on:
            self._backup_all(games, silent=silent)
            return
        self._sync_games(games, silent=silent)

    def _backup_all(self, games, silent=False):
        """本地备份模式下的「同步全部」：逐款存快照，不碰任何远端。"""
        self._set_busy(True, "开始本地备份 %d 款游戏…" % len(games))
        self._backup_queue = list(games)
        self._backup_silent = silent
        self._backup_next()

    def _backup_next(self):
        if not self._backup_queue:
            self._set_busy(False)
            self._log("全部本地备份完成")
            self.refresh_all()
            if not self._backup_silent:
                QMessageBox.information(self, "备份完成",
                                        "所有游戏已备份到本地保险库。")
            return
        g = self._backup_queue.pop(0)

        def job(sig):
            return self.vault.create_snapshot(
                g, kind="auto", note="本地备份",
                progress_cb=lambda c, t: sig.progress.emit("备份 %s" % g.name, c, t))

        def done(snap):
            self._log("%s 已备份：%d 个文件 / %s"
                      % (g.name, len(snap.files), _human(snap.total_size)))
            self._prune(g)
            self._backup_next()

        run_async(job, on_result=done, on_progress=self._on_progress,
                  on_error=lambda e: (self._set_busy(False),
                                      self._err_bg(e) if self._backup_silent
                                      else self._err(e)))

    def _sync_games(self, games, silent=False):
        self._set_busy(True, "开始同步 %d 款游戏…" % len(games))
        self._sync_queue = list(games)
        self._sync_silent = silent
        self._sync_next()

    def _sync_next(self):
        if not self._sync_queue:
            self._set_busy(False)
            self._log("全部同步完成")
            self.refresh_all()
            if not self._sync_silent:
                QMessageBox.information(self, "同步完成", "所有游戏已同步。")
            return
        g = self._sync_queue.pop(0)

        def job(sig):
            plan = self.engine.plan(g)
            if plan.error:
                return g, plan, None, plan.error
            if plan.has_conflict:
                return g, plan, None, "conflict"
            res = self.engine.execute(g, plan, {},
                                      progress_cb=lambda stage, p: sig.progress.emit(
                                          "%s %s" % (stage, g.name), 0, 0))
            return g, plan, res, ""

        def done(res):
            g, plan, r, flag = res
            if flag == "conflict":
                self._set_busy(False)
                self._handle_conflicts(g, plan)
                return
            if r and r.get("errors"):
                for e in r["errors"]:
                    self._log("! %s" % e)
                self._set_busy(False)
                if not self._sync_silent:
                    QMessageBox.warning(self, "同步未完成",
                                        "%s 有 %d 项失败，已保留现场，下次重试：\n%s"
                                        % (g.name, len(r["errors"]), "\n".join(r["errors"][:5])))
                self.refresh_all()
                return
            if r:
                self._log("%s 同步完成：上传 %d chunk / 下载 %d chunk"
                          % (g.name, r["uploaded"], r["downloaded"]))
            else:
                self._log("%s 无需同步" % g.name)
            self._prune(g)
            self._sync_next()

        run_async(job, on_result=done, on_progress=self._on_progress,
                  on_error=lambda e: (self._set_busy(False), self._err(e)))

    def _handle_conflicts(self, g, plan):
        dlg = ConflictDialog(g.name, plan.conflicts, self)
        if dlg.exec() != ConflictDialog.Accepted:
            self._log("%s 冲突未处理，同步暂停" % g.name)
            self.refresh_all()
            return
        self._set_busy(True, "正在按裁决同步 %s …" % g.name)
        resolutions = dlg.get_resolutions()

        def job(sig):
            return self.engine.execute(g, plan, resolutions)

        def done(r):
            self._set_busy(False)
            if r.get("errors"):
                self._err("同步失败：\n" + "\n".join(r["errors"][:5]))
            else:
                self._log("%s 冲突已按你的选择处理" % g.name)
            self.refresh_all()

        run_async(job, on_result=done,
                  on_error=lambda e: (self._set_busy(False), self._err(e)))

    # ---------- 历史版本 / 回滚 / 差异 ----------
    def show_history(self):
        g = self.current_game()
        if not g:
            return
        snaps = self.vault.list_snapshots(g.id)
        if not snaps:
            QMessageBox.information(self, "没有历史版本", "请先执行一次备份。")
            return
        HistoryDialog(g, snaps, self._do_rollback, self._show_diff, self).exec()

    def _show_diff(self, old, new):
        DiffDialog(old, new, self).exec()

    def _do_rollback(self, snap):
        g = self.current_game()
        push = ("回滚后本地内容与该版本完全一致，随后同步会把此版本推送到云端。"
                if self.cloud_on else
                "当前为本地备份模式，回滚只影响本地目录与本地保险库。")
        detail = ("将把 %s 回滚到 %s 的快照：\n\n"
                  "  版本时间：%s\n"
                  "  文件数量：%d\n"
                  "  总体积：%s\n\n"
                  "当前存档目录里被覆盖的文件，会先自动复制到 "
                  "本地保险库的 localcopies/ 留底。\n%s"
                  % (g.name, _fmt(snap.created_at), _fmt(snap.created_at),
                     len(snap.files), _human(snap.total_size), push))
        if not ConfirmDialog.ask(
                "回滚存档 —— %s" % g.name,
                "此操作会用旧版本覆盖当前存档，属于不可逆操作。",
                detail, require_check=True, parent=self):
            return
        self._set_busy(True, "正在回滚…")

        def job(sig):
            return self.vault.restore_snapshot(g, snap.id,
                                               safety=self.cfg.safety["keep_local_copy"],
                                               progress_cb=lambda c, t: sig.progress.emit(
                                                   "回滚", c, t))

        def done(st):
            self._log("回滚完成：还原 %d 个文件，覆盖前留底 %d 个"
                      % (st["restored"], st["backed_up"]))
            self._prune(g)
            self._set_busy(False)
            self.refresh_all()
            QMessageBox.information(self, "回滚完成",
                                    "已还原 %d 个文件（%s）。\n%d 个被覆盖的文件已留底在 "
                                    "localcopies/ 目录。" % (st["restored"],
                                                         _human(st["bytes"]), st["backed_up"]))

        run_async(job, on_result=done, on_progress=self._on_progress,
                  on_error=lambda e: (self._set_busy(False), self._err(e)))

    # ---------- 查看文件 ----------
    def show_files(self):
        g = self.current_game()
        if not g:
            return
        snaps = self.vault.list_snapshots(g.id)
        if not snaps:
            QMessageBox.information(self, "暂无数据", "请先执行一次备份。")
            return
        s = snaps[0]
        from .dialogs import DiffDialog  # 复用表格展示
        dlg = QDialog(self)
        dlg.setWindowTitle("%s —— 备份内容" % g.name)
        dlg.resize(700, 520)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel("最新快照 %s 共 %d 个文件 / %s"
                             % (_fmt(s.created_at), len(s.files), _human(s.total_size))))
        t = QTableWidget(0, 3)
        t.setHorizontalHeaderLabels(["文件", "大小", "chunk 数"])
        t.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.setRowCount(len(s.files))
        for r, e in enumerate(s.files):
            t.setItem(r, 0, QTableWidgetItem(e.path))
            t.setItem(r, 1, QTableWidgetItem(_human(e.size)))
            t.setItem(r, 2, QTableWidgetItem(str(len(e.chunks))))
        lay.addWidget(t)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        dlg.exec()

    # ---------- 添加 / 移除 ----------
    def add_game(self):
        self._set_busy(True, "正在扫描本机存档目录…")
        known = [p for g in self.vault.list_games() for p in g.paths]

        def job(sig):
            return scanner.scan(known_paths=known)

        def done(found):
            self._set_busy(False)
            if not found:
                QMessageBox.information(self, "没有发现",
                                        "没有扫描到新的存档目录，可用「添加自定义」手动指定。")
            dlg = AddGameDialog(found, self)
            if dlg.exec() != AddGameDialog.Accepted:
                return
            picked = dlg.get_selected()
            for fg in picked:
                g = self.vault.add_game(fg.name, fg.paths, platform=fg.platform)
                self._log("已添加游戏：%s（%s）" % (g.name, " / ".join(g.paths)))
            self.refresh_all()

        run_async(job, on_result=done,
                  on_error=lambda e: (self._set_busy(False), self._err(e)))

    def remove_game(self):
        g = self.current_game()
        if not g:
            return
        if not ConfirmDialog.ask("移除游戏 —— %s" % g.name,
                                 "将从列表移除该游戏，已备份的快照与 chunk 会保留在保险库中。",
                                 "游戏：%s\n路径：%s\n\n这不会删除任何已备份的数据，"
                                 "之后可以重新添加。" % (g.name, "\n".join(g.paths)),
                                 require_check=True, parent=self):
            return
        self.vault.remove_game(g.id, keep_snapshots=True)
        self._log("已移除游戏：%s" % g.name)
        self.refresh_all()

    # ---------- 设置 ----------
    def open_about(self):
        AboutDialog(self).exec()

    def open_settings(self):
        dlg = SettingsDialog(self.cfg, self)
        if dlg.exec() != SettingsDialog.Accepted:
            return
        old_remote = self.cfg.remote_conf.get("path")
        old_vault = self.cfg.vault_root
        old_cloud = self.cloud_on
        dlg.apply()
        self._log("设置已保存")
        if old_vault != self.cfg.vault_root:
            QMessageBox.information(self, "需要重启",
                                    "本地保险库目录已更改，请重启应用生效。")
            return
        if old_cloud != self.cloud_on or old_remote != self.cfg.remote_conf.get("path"):
            self._rebuild_backend()
        self._apply_schedule()
        self.refresh_all()

    # ==========================================================
    def closeEvent(self, ev):
        ev.ignore()
        self.hide()
        self.tray.showMessage("GameSaveCloud", "已最小化到托盘，仍在后台定时同步。",
                              QSystemTrayIcon.Information, 2000)
