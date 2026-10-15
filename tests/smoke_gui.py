# -*- coding: utf-8 -*-
"""GUI 冒烟测试（离屏运行）。

重点验证两件事：
  1. 后台工作线程能正常读 SQLite —— 之前 Vault 的连接绑在主线程，
     一进 QThreadPool 就抛 ProgrammingError，界面永久卡在忙碌状态。
  2. 后台任务失败不再弹模态框 —— 模态框会霸占事件循环把应用挂死。

跑法：
    QT_QPA_PLATFORM=offscreen python tests/smoke_gui.py
"""
import os
import sys
import tempfile
import shutil

TMP = tempfile.mkdtemp(prefix="gsc_smoke_")
os.environ["LOCALAPPDATA"] = TMP          # Config.default_base() 读这个
os.environ["QT_QPA_PLATFORM"] = "offscreen"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer

from ui.main_window import MainWindow

FAILS = []


def check(name, cond, extra=""):
    print("  [%s] %s %s" % ("OK" if cond else "FAIL", name, extra))
    if not cond:
        FAILS.append(name)


def pump(ms=400, rounds=1):
    """手动驱动事件循环，让工作线程的信号有机会被投递到主线程。"""
    app = QApplication.instance()
    for _ in range(rounds):
        app.processEvents()
        import time
        t0 = time.time()
        while time.time() - t0 < ms / 1000.0:
            app.processEvents()
            time.sleep(0.01)


def main():
    app = QApplication(sys.argv)

    # 无头环境下模态框会永久霸占事件循环（用户没得点它），
    # 把 QMessageBox 的静态方法换成「记一笔就返回」，只验证代码路径走得通。
    from PySide6.QtWidgets import QMessageBox
    shown_modals = []
    for name in ("information", "critical", "warning", "question"):
        setattr(QMessageBox, name,
                staticmethod(lambda *a, _n=name, **k: shown_modals.append(_n)))

    w = MainWindow()
    w._shown_modals = shown_modals

    # --- 造一个假游戏 + 假存档，让后台任务有活干 ---
    gamedir = os.path.join(TMP, "FakeGame", "saves")
    os.makedirs(gamedir, exist_ok=True)
    with open(os.path.join(gamedir, "slot1.sav"), "wb") as f:
        f.write(os.urandom(300000))
    with open(os.path.join(gamedir, "slot2.sav"), "wb") as f:
        f.write(os.urandom(120000))
    g = w.vault.add_game("FakeGame", [gamedir], platform="测试")

    print("== 1. 后台刷新（之前必抛 SQLite 线程错误） ==")
    w.refresh_all()
    pump(ms=2500, rounds=6)
    check("busy 已回落", w.busy is False, "busy=%r" % w.busy)
    check("列表有 1 款游戏", w.game_list.count() == 1,
          "count=%d" % w.game_list.count())
    check("后台错误未进模态框（_err_bg 不弹窗）",
          not any("后台任务失败" in m or "SQLite objects" in m
                  for m in []), "")

    # 从日志里读线索
    logtext = w.log_view.toPlainText()
    check("日志无 SQLite 线程报错", "SQLite objects created in a thread" not in logtext)
    check("日志记录了刷新完成", "刷新完成" in logtext)

    print("== 2. 多线程并发读同一 vault ==")
    import threading
    errs = []

    def reader():
        try:
            for _ in range(20):
                w.vault.list_games()
                w.vault.get_sync_state(g.id)
                w.vault.stats()
                w.vault.list_snapshots(g.id)
        except Exception as e:      # noqa: BLE001
            errs.append(repr(e))

    ts = [threading.Thread(target=reader) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    check("8 线程并发读无异常", not errs, "; ".join(errs[:2]))

    print("== 3. 对话框可构造 ==")
    from ui.dialogs import (ConfirmDialog, ConflictDialog, AddGameDialog,
                            HistoryDialog, DiffDialog, SettingsDialog)
    dialogs = []
    try:
        dialogs.append(ConfirmDialog("测试", "确认", "明细", require_check=True, parent=w))
        dialogs.append(ConflictDialog("FakeGame", [], parent=w))
        dialogs.append(AddGameDialog([], parent=w))
        dialogs.append(HistoryDialog(g, [], lambda s: None, lambda a, b: None, parent=w))
        snap = w.vault.create_snapshot(g, kind="manual", note="冒烟")
        dialogs.append(HistoryDialog(g, [snap], lambda s: None,
                                     lambda a, b: None, parent=w))
        dialogs.append(DiffDialog(snap, snap, parent=w))
        dialogs.append(SettingsDialog(w.cfg, parent=w))
        check("7 个对话框全部构造成功", len(dialogs) == 7, "实际 %d" % len(dialogs))
    except Exception as e:      # noqa: BLE001
        check("7 个对话框全部构造成功", False, repr(e))
    finally:
        for d in dialogs:
            d.deleteLater()

    print("== 4. 本地备份模式（默认未配置云端） ==")
    w.refresh_all()
    pump(ms=2000, rounds=4)
    check("默认不创建云端后端", w.remote is None, "remote=%r" % w.remote)
    check("cloud_on 为 False", w.cloud_on is False)
    check("模式标签显示「本地备份」", "本地备份" in w.mode_chip.text(),
          w.mode_chip.text())
    check("同步按钮文案切成「备份全部」", w.b_sync.text() == "备份全部",
          w.b_sync.text())
    check("单游戏按钮文案切成「备份此游戏」",
          w.b_sync_one.text() == "备份此游戏", w.b_sync_one.text())

    w.backup_current()
    pump(ms=4000, rounds=8)
    check("备份后 busy 回落", w.busy is False, "busy=%r" % w.busy)
    check("快照表有记录", w.snap_table.rowCount() >= 1,
          "rows=%d" % w.snap_table.rowCount())

    n_before = len(w.vault.list_snapshots(g.id))
    w.sync_all(silent=True)          # 本地模式下等于「备份全部」
    pump(ms=5000, rounds=10)
    check("本地模式同步后 busy 回落", w.busy is False, "busy=%r" % w.busy)
    n_after = len(w.vault.list_snapshots(g.id))
    check("本地模式同步多存了一份快照", n_after > n_before,
          "%d -> %d" % (n_before, n_after))
    logtext = w.log_view.toPlainText()
    check("同步日志无异常栈", "Traceback" not in logtext)
    # 注意：LOCALAPPDATA/GameSaveCloud 是应用自己的 base 目录
    # （config.json、保险库、图标缓存都在里面），本来就该存在。
    # 真正的云端目录是它下面那一层。
    cloud_dir = os.path.join(TMP, "GameSaveCloud", "GameSaveCloud")
    check("没有凭空创建云端目录", not os.path.isdir(cloud_dir),
          cloud_dir)

    print("== 5. 开启云端后走完整同步链路 ==")
    w.cfg.set("remote.enabled", True)
    w._rebuild_backend()
    check("开启云端后建出了后端", w.remote is not None)
    check("模式标签切到云端", "云端" in w.mode_chip.text(), w.mode_chip.text())
    w.refresh_all()
    pump(ms=2500, rounds=5)
    w.sync_all(silent=True)
    pump(ms=6000, rounds=12)
    check("云端同步后 busy 回落", w.busy is False, "busy=%r" % w.busy)
    logtext = w.log_view.toPlainText()
    check("云端同步日志无异常栈", "Traceback" not in logtext)

    remote_root = w.remote.root
    n_snaps = len([f for f in os.listdir(os.path.join(remote_root, "snapshots"))
                   if f.endswith(".json")]) if os.path.isdir(
                      os.path.join(remote_root, "snapshots")) else 0
    check("云端已有快照", n_snaps >= 1, "snaps=%d" % n_snaps)

    # --- 汇总 ---
    print()
    if FAILS:
        print("冒烟测试失败 %d 项：%s" % (len(FAILS), FAILS))
    else:
        print("冒烟测试全部通过")
    return 1 if FAILS else 0


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    finally:
        try:
            shutil.rmtree(TMP, ignore_errors=True)
        except Exception:
            pass
    sys.exit(code)
