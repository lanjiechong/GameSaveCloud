# -*- coding: utf-8 -*-
"""
对话框集合。危险操作一律走「明确后果 + 默认否」的二次确认。
"""
import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QColor
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QListWidget,
    QListWidgetItem, QTableWidget, QTableWidgetItem, QHeaderView, QCheckBox,
    QLineEdit, QSpinBox, QComboBox, QFileDialog, QGroupBox, QFormLayout,
    QTextEdit, QDialogButtonBox, QMessageBox, QAbstractItemView, QSplitter,
    QWidget, QRadioButton, QButtonGroup,
)

from .theme import PALETTE, state_color, state_bg
from gamesave import chunker
from gamesave.models import (
    RESOLVE_KEEP_LOCAL, RESOLVE_USE_REMOTE, RESOLVE_SAVE_BOTH)


def _fmt(ts):
    if not ts:
        return "—"
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _human(n):
    return chunker.human_size(n)


# ==================================================================
# 危险操作二次确认
# ==================================================================
class ConfirmDialog(QDialog):
    """破坏性操作确认。标题写明后果，默认焦点在「取消」。"""

    def __init__(self, title, warning, detail="", require_check=False,
                 check_text="我已了解上述后果，仍要继续", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        lay.setSpacing(12)

        lbl = QLabel(warning)
        lbl.setWordWrap(True)
        lbl.setStyleSheet("font-size:14px; font-weight:600; color:%s;" % PALETTE["danger"])
        lay.addWidget(lbl)

        if detail:
            box = QTextEdit()
            box.setReadOnly(True)
            box.setMaximumHeight(180)
            box.setPlainText(detail)
            lay.addWidget(box)

        self.check = None
        if require_check:
            self.check = QCheckBox(check_text)
            lay.addWidget(self.check)

        btns = QDialogButtonBox(QDialogButtonBox.Yes | QDialogButtonBox.No)
        btns.button(QDialogButtonBox.Yes).setText("继续")
        btns.button(QDialogButtonBox.No).setText("取消")
        btns.button(QDialogButtonBox.No).setDefault(True)
        btns.button(QDialogButtonBox.No).setFocus()
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)
        self._btns = btns

    def _on_accept(self):
        if self.check is not None and not self.check.isChecked():
            QMessageBox.information(self, "需要确认", "请先勾选确认框，再点击「继续」。")
            return
        self.accept()

    @staticmethod
    def ask(title, warning, detail="", require_check=False, parent=None) -> bool:
        d = ConfirmDialog(title, warning, detail, require_check, parent)
        return d.exec() == QDialog.Accepted


# ==================================================================
# 冲突解决
# ==================================================================
class ConflictDialog(QDialog):
    """逐个文件列出冲突，让用户为每个文件选择保留哪一边。"""

    CHOICES = [
        (RESOLVE_KEEP_LOCAL, "保留本地"),
        (RESOLVE_USE_REMOTE, "使用云端"),
        (RESOLVE_SAVE_BOTH, "另存为新版本"),
    ]

    def __init__(self, game_name, conflicts, parent=None):
        super().__init__(parent)
        self.setWindowTitle("存档冲突 —— %s" % game_name)
        self.resize(760, 560)
        self.conflicts = conflicts
        self.resolutions = {}

        lay = QVBoxLayout(self)
        intro = QLabel(
            "以下文件在<b>本地</b>和<b>云端</b>都被修改过，且内容不一致。\n"
            "自动同步已暂停。请为每个文件选择处理方式，选择后才会继续同步。")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["文件", "本地", "云端", "处理方式"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Fixed)
        self.table.setColumnWidth(3, 300)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        lay.addWidget(self.table)

        self._fill()

        tip = QLabel(
            "<b>保留本地</b>：用本地版本覆盖云端，本地文件不变。<br>"
            "<b>使用云端</b>：用云端版本覆盖本地，覆盖前会自动在本地留底。<br>"
            "<b>另存为新版本</b>：本地版本保持为主文件，云端版本另存为 "
            "「文件名.云端版本_时间戳」，两边都不丢。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:%s;" % PALETTE["muted"])
        lay.addWidget(tip)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Ok).setText("按上述选择同步")
        btns.button(QDialogButtonBox.Cancel).setText("暂不处理")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _fill(self):
        self.table.setRowCount(len(self.conflicts))
        self._groups = []
        for r, c in enumerate(self.conflicts):
            self.table.setItem(r, 0, QTableWidgetItem(c.path))
            self.table.setItem(r, 1, QTableWidgetItem(
                "%s\n%s" % (_human(c.local_size), _fmt(c.local_mtime))))
            self.table.setItem(r, 2, QTableWidgetItem(
                "%s\n%s" % (_human(c.remote_size), _fmt(c.remote_mtime))))
            holder = QWidget()
            h = QHBoxLayout(holder)
            h.setContentsMargins(4, 2, 4, 2)
            h.setSpacing(10)
            grp = QButtonGroup(self)
            for i, (val, text) in enumerate(self.CHOICES):
                rb = QRadioButton(text)
                grp.addButton(rb, i)
                h.addWidget(rb)
                if i == 0:
                    rb.setChecked(True)
            grp.idClicked.connect(self._choose)
            self._groups.append((c.path, grp))
            self.table.setCellWidget(r, 3, holder)
            self.table.setRowHeight(r, 46)
            self.resolutions[c.path] = RESOLVE_KEEP_LOCAL
        self.table.resizeRowsToContents()

    def _choose(self, _id):
        for path, grp in self._groups:
            if grp is self.sender():
                self.resolutions[path] = self.CHOICES[grp.checkedId()][0]
                return

    def get_resolutions(self):
        return dict(self.resolutions)


# ==================================================================
# 添加游戏（扫描结果 + 自定义路径）
# ==================================================================
class AddGameDialog(QDialog):
    def __init__(self, found_games, parent=None):
        super().__init__(parent)
        self.setWindowTitle("添加游戏存档")
        self.resize(720, 560)
        self.selected = None

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("扫描到以下可能含存档的目录，勾选后添加："))

        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.NoSelection)
        lay.addWidget(self.list)
        for fg in found_games:
            src = "查表" if fg.source == "known" else "启发式"
            text = "%s\n    %s  ·  %d 个文件  ·  %s  ·  %s" % (
                fg.name, " / ".join(fg.paths), fg.files, _human(fg.size), src)
            it = QListWidgetItem(text)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Unchecked)
            it.setData(Qt.UserRole, fg)
            self.list.addItem(it)

        row = QHBoxLayout()
        self.custom = QLineEdit()
        self.custom.setPlaceholderText("或手动输入存档目录绝对路径，例如 C:\\Users\\me\\Documents\\My Games\\XXX")
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse)
        addc = QPushButton("添加自定义")
        addc.clicked.connect(self._add_custom)
        row.addWidget(self.custom)
        row.addWidget(browse)
        row.addWidget(addc)
        lay.addLayout(row)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Ok).setText("添加勾选项")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "选择存档目录")
        if d:
            self.custom.setText(d)

    def _add_custom(self):
        p = self.custom.text().strip()
        if not p or not os.path.isdir(p):
            QMessageBox.warning(self, "路径无效", "请输入一个已存在的目录。")
            return
        from gamesave.scanner import FoundGame
        fg = FoundGame(os.path.basename(p.rstrip("\\/")), "自定义", [p], 1, 0, "custom")
        it = QListWidgetItem("%s\n    %s  ·  自定义" % (fg.name, p))
        it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
        it.setCheckState(Qt.Checked)
        it.setData(Qt.UserRole, fg)
        self.list.addItem(it)
        self.custom.clear()

    def get_selected(self):
        out = []
        for i in range(self.list.count()):
            it = self.list.item(i)
            if it.checkState() == Qt.Checked:
                out.append(it.data(Qt.UserRole))
        return out


# ==================================================================
# 历史版本 / 差异对比 / 回滚
# ==================================================================
class HistoryDialog(QDialog):
    def __init__(self, game, snapshots, on_rollback, on_diff, parent=None):
        super().__init__(parent)
        self.setWindowTitle("历史版本 —— %s" % game.name)
        self.resize(860, 600)
        self.snapshots = snapshots
        self.on_rollback = on_rollback
        self.on_diff = on_diff

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("每次备份都会保留一份完整快照。chunk 级去重让历史版本"
                             "几乎不占额外空间。选择两条记录可对比差异。"))

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["时间", "类型", "文件数", "体积", "说明"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        lay.addWidget(self.table)
        self._fill()

        row = QHBoxLayout()
        b_diff = QPushButton("与上一版对比差异")
        b_diff.clicked.connect(self._diff)
        b_roll = QPushButton("回滚到所选版本")
        b_roll.setObjectName("Danger")
        b_roll.clicked.connect(self._rollback)
        row.addStretch(1)
        row.addWidget(b_diff)
        row.addWidget(b_roll)
        lay.addLayout(row)

        btns = QDialogButtonBox(QDialogButtonBox.Close)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _fill(self):
        self.table.setRowCount(len(self.snapshots))
        for r, s in enumerate(self.snapshots):
            self.table.setItem(r, 0, QTableWidgetItem(_fmt(s.created_at)))
            kind = {"manual": "手动", "auto": "自动", "sync": "同步",
                    "restore": "回滚"}.get(s.kind, s.kind)
            self.table.setItem(r, 1, QTableWidgetItem(kind))
            self.table.setItem(r, 2, QTableWidgetItem(str(len(s.files))))
            self.table.setItem(r, 3, QTableWidgetItem(_human(s.total_size)))
            self.table.setItem(r, 4, QTableWidgetItem(s.note or ""))
            self.table.item(r, 0).setData(Qt.UserRole, s)

    def _current(self):
        r = self.table.currentRow()
        if r < 0:
            return None
        return self.table.item(r, 0).data(Qt.UserRole)

    def _diff(self):
        s = self._current()
        if not s:
            QMessageBox.information(self, "未选择", "请先选择一行。")
            return
        idx = self.snapshots.index(s)
        prev = self.snapshots[idx + 1] if idx + 1 < len(self.snapshots) else None
        if not prev:
            QMessageBox.information(self, "无法对比", "这是最早的一版，没有可对比的对象。")
            return
        self.on_diff(prev, s)

    def _rollback(self):
        s = self._current()
        if not s:
            QMessageBox.information(self, "未选择", "请先选择要回滚到的版本。")
            return
        self.on_rollback(s)


class DiffDialog(QDialog):
    def __init__(self, old, new, parent=None):
        super().__init__(parent)
        self.setWindowTitle("版本差异")
        self.resize(760, 520)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("<b>旧版</b> %s（%d 文件 / %s）　→　"
                             "<b>新版</b> %s（%d 文件 / %s）"
                             % (_fmt(old.created_at), len(old.files), _human(old.total_size),
                                _fmt(new.created_at), len(new.files), _human(new.total_size))))
        old_map = {e.path: e for e in old.files}
        new_map = {e.path: e for e in new.files}
        added = sorted(set(new_map) - set(old_map))
        removed = sorted(set(old_map) - set(new_map))
        changed = sorted(p for p in set(old_map) & set(new_map)
                         if old_map[p].sha256 != new_map[p].sha256)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["变更", "文件", "旧", "新"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        lay.addWidget(self.table)

        rows = ([("新增", p, "—", _human(new_map[p].size)) for p in added] +
                [("删除", p, _human(old_map[p].size), "—") for p in removed] +
                [("修改", p, _human(old_map[p].size), _human(new_map[p].size))
                 for p in changed])
        self.table.setRowCount(len(rows))
        for r, (kind, p, a, b) in enumerate(rows):
            color = {"新增": PALETTE["synced"], "删除": PALETTE["conflict"],
                     "修改": PALETTE["local"]}[kind]
            it = QTableWidgetItem(kind)
            it.setForeground(QColor(color))
            self.table.setItem(r, 0, it)
            self.table.setItem(r, 1, QTableWidgetItem(p))
            self.table.setItem(r, 2, QTableWidgetItem(a))
            self.table.setItem(r, 3, QTableWidgetItem(b))

        if not rows:
            lay.addWidget(QLabel("两个版本内容完全一致。"))

        btns = QDialogButtonBox(QDialogButtonBox.Close)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)


# ==================================================================
# 设置
# ==================================================================
class SettingsDialog(QDialog):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumWidth(620)
        self.cfg = cfg
        d = cfg.data

        lay = QVBoxLayout(self)

        # 云端后端
        g1 = QGroupBox("云端同步（可选）")
        f1 = QFormLayout(g1)
        self.cloud_on = QCheckBox("启用云端同步")
        self.cloud_on.setChecked(bool(d["remote"].get("enabled")))
        f1.addRow("", self.cloud_on)
        f1.addRow("", QLabel("默认只做本地备份，存进本机保险库。\n"
                             "只有勾选这里并填好下面的位置，才会把存档同步到云端。"))
        self.backend = QComboBox()
        self.backend.addItem("本地目录 / 移动硬盘 / NAS 挂载", "local_folder")
        self.backend.addItem("WebDAV（坚果云 / Nextcloud / 群晖）", "webdav")
        idx = self.backend.findData(d["remote"]["type"])
        self.backend.setCurrentIndex(max(idx, 0))
        self.backend.currentIndexChanged.connect(self._toggle_backend)
        f1.addRow("后端类型", self.backend)

        self.remote_path = QLineEdit(d["remote"]["path"])
        brow = QPushButton("浏览…")
        brow.clicked.connect(self._browse_remote)
        h1 = QHBoxLayout()
        h1.addWidget(self.remote_path)
        h1.addWidget(brow)
        f1.addRow("目录", _wrap(h1))

        self.dav_url = QLineEdit(d["remote"]["url"])
        self.dav_url.setPlaceholderText("https://dav.example.com/dav/GameSaveCloud")
        f1.addRow("WebDAV 地址", self.dav_url)
        self.dav_user = QLineEdit(d["remote"]["user"])
        f1.addRow("账号", self.dav_user)
        # 显示的是内存中解密的密码；配置文件里只保存 DPAPI 密文
        self.dav_pass = QLineEdit(cfg.get_remote_password())
        self.dav_pass.setEchoMode(QLineEdit.Password)
        f1.addRow("密码 / 应用授权码", self.dav_pass)
        lay.addWidget(g1)

        # 本地保险库
        g2 = QGroupBox("本地保险库")
        f2 = QFormLayout(g2)
        self.vault = QLineEdit(d["vault_root"])
        brow2 = QPushButton("浏览…")
        brow2.clicked.connect(self._browse_vault)
        h2 = QHBoxLayout()
        h2.addWidget(self.vault)
        h2.addWidget(brow2)
        f2.addRow("目录", _wrap(h2))
        f2.addRow("", QLabel("快照清单、chunk 去重存储、覆盖前的安全留底都在这里。"))
        lay.addWidget(g2)

        # 定时备份
        g3 = QGroupBox("定时自动备份")
        f3 = QFormLayout(g3)
        self.sched_on = QCheckBox("启用定时同步")
        self.sched_on.setChecked(d["schedule"]["enabled"])
        f3.addRow("", self.sched_on)
        self.sched_min = QSpinBox()
        self.sched_min.setRange(5, 1440)
        self.sched_min.setValue(int(d["schedule"]["interval_minutes"]))
        f3.addRow("间隔（分钟）", self.sched_min)
        self.sched_silent = QCheckBox("静默同步（不弹窗，只在日志记录）")
        self.sched_silent.setChecked(d["schedule"]["silent"])
        f3.addRow("", self.sched_silent)
        self.autostart = QCheckBox("开机时自动启动（最小化到系统托盘）")
        self.autostart.setChecked(bool(d["schedule"].get("autostart")))
        f3.addRow("", self.autostart)
        lay.addWidget(g3)

        # 安全
        g4 = QGroupBox("安全")
        f4 = QFormLayout(g4)
        self.keep_copy = QCheckBox("覆盖 / 回滚前自动在本地留存副本")
        self.keep_copy.setChecked(d["safety"]["keep_local_copy"])
        f4.addRow("", self.keep_copy)
        self.keep_snaps = QSpinBox()
        self.keep_snaps.setRange(1, 200)
        self.keep_snaps.setValue(int(d["safety"]["keep_snapshots"]))
        f4.addRow("每款游戏保留快照数", self.keep_snaps)
        self.confirm_danger = QCheckBox("危险操作二次确认")
        self.confirm_danger.setChecked(d["safety"]["confirm_dangerous"])
        f4.addRow("", self.confirm_danger)
        lay.addWidget(g4)

        self._btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self._btns.accepted.connect(self._validate_accept)
        self._btns.rejected.connect(self.reject)
        lay.addWidget(self._btns)
        self.cloud_on.toggled.connect(self._toggle_backend)
        self._toggle_backend()

    def _validate_accept(self):
        """确定前的硬性校验。当前只有一条：启用 WebDAV 且用 http:// 时
        明确警告账号密码与存档会明文传输，需二次确认。"""
        if (self.cloud_on.isChecked()
                and self.backend.currentData() == "webdav"
                and self.dav_url.text().strip().lower().startswith("http://")):
            r = QMessageBox.warning(
                self, "明文连接警告",
                "WebDAV 地址是 http:// 而非 https://。\n\n"
                "账号、密码以及存档内容都将以明文在网络上传输，"
                "同一网络中的其他人可以截获。\n\n确定仍要使用这个地址吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if r != QMessageBox.Yes:
                return
        self.accept()

    def _toggle_backend(self):
        enabled = self.cloud_on.isChecked()
        is_dav = self.backend.currentData() == "webdav"
        self.backend.setEnabled(enabled)
        self.remote_path.setEnabled(enabled and not is_dav)
        self.dav_url.setEnabled(enabled and is_dav)
        self.dav_user.setEnabled(enabled and is_dav)
        self.dav_pass.setEnabled(enabled and is_dav)

    def _browse_remote(self):
        d = QFileDialog.getExistingDirectory(self, "选择云端目录")
        if d:
            self.remote_path.setText(d)

    def _browse_vault(self):
        d = QFileDialog.getExistingDirectory(self, "选择本地保险库目录")
        if d:
            self.vault.setText(d)

    def apply(self):
        c = self.cfg
        c.set("remote.enabled", self.cloud_on.isChecked())
        c.set("remote.type", self.backend.currentData())
        c.set("remote.path", self.remote_path.text().strip())
        c.set("remote.url", self.dav_url.text().strip())
        c.set("remote.user", self.dav_user.text().strip())
        # 密码走 DPAPI 加密后再落盘，config.json 里不留明文
        c.set_remote_password(self.dav_pass.text())
        c.set("vault_root", self.vault.text().strip())
        c.set("schedule.enabled", self.sched_on.isChecked())
        c.set("schedule.interval_minutes", self.sched_min.value())
        c.set("schedule.silent", self.sched_silent.isChecked())
        # 开机自启：同步 HKCU Run 项；非 Windows 平台 set_enabled 返回 False
        want_autostart = self.autostart.isChecked()
        c.set("schedule.autostart",
              want_autostart and self._apply_autostart(want_autostart))
        c.set("safety.keep_local_copy", self.keep_copy.isChecked())
        c.set("safety.keep_snapshots", self.keep_snaps.value())
        c.set("safety.confirm_dangerous", self.confirm_dangerous.isChecked())

    @staticmethod
    def _apply_autostart(on: bool) -> bool:
        try:
            from . import autostart
            return autostart.set_enabled(on)
        except Exception:
            return False


class _Wrap(QWidget):
    def __init__(self, layout):
        super().__init__()
        self.setLayout(layout)


def _wrap(layout):
    return _Wrap(layout)


# ==================================================================
# 关于
# ==================================================================
class AboutDialog(QDialog):
    """版本与项目信息。发布版的「门面」，信息保持克制：版本 + 一句简介 + 链接。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        from version import (APP_NAME, APP_TAGLINE, VERSION, AUTHOR,
                             REPO_URL, ISSUES_URL)
        self.setWindowTitle("关于 %s" % APP_NAME)
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        title = QLabel("%s v%s" % (APP_NAME, VERSION))
        title.setStyleSheet("font-size:20px; font-weight:700;")
        lay.addWidget(title)

        sub = QLabel(APP_TAGLINE)
        sub.setStyleSheet("font-size:13px; color:%s;" % PALETTE["muted"])
        lay.addWidget(sub)

        box = QTextEdit()
        box.setReadOnly(True)
        box.setMaximumHeight(150)
        box.setPlainText(
            "本机游戏存档的备份、历史版本、回滚与多设备同步工具。\n"
            "备份、历史、回滚、冲突检测全部在本地完成；\n"
            "只有在设置里勾选「启用云端同步」后才会访问网络。\n"
            "\n"
            "· 备份引擎：分块 + zstd 压缩 + 内容寻址去重\n"
            "· 冲突处理：keep_local / use_remote / save_both 三种策略\n"
            "· 调度：定时备份、托盘常驻、开机自启可选\n"
            "· 游戏图标：内置 Steam 周销量榜 Top100 真实 logo 包，离线可用")
        lay.addWidget(box)

        info = QLabel("作者：%s" % AUTHOR)
        info.setStyleSheet("color:%s;" % PALETTE["muted"])
        lay.addWidget(info)

        links = QHBoxLayout()
        for text, url in (("项目主页", REPO_URL), ("反馈问题", ISSUES_URL)):
            b = QPushButton(text)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, u=url: _open_url(u))
            links.addWidget(b)
        links.addStretch(1)
        lay.addLayout(links)

        btns = QDialogButtonBox(QDialogButtonBox.Close)
        btns.rejected.connect(self.reject)
        btns.accepted.connect(self.accept)
        lay.addWidget(btns)


def _open_url(url):
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtCore import QUrl
    QDesktopServices.openUrl(QUrl(url))
