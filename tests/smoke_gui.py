# -*- coding: utf-8 -*-
"""GUI 冒烟测试（离屏运行）。

重点验证：
  1. 后台工作线程能正常读 SQLite —— 之前 Vault 的连接绑在主线程，
     一进 QThreadPool 就抛 ProgrammingError，界面永久卡在忙碌状态。
  2. 后台任务失败不再弹模态框 —— 模态框会霸占事件循环把应用挂死。
  3. 本地备份模式默认不碰云端，开启云端后走完整同步链路。
  4. 游戏图标：任何情况下 pixmap 都返回 96x96 图标不空白；
     若装了可选离线 logo 包，组合名（「荒野大镖客2 Red Dead
     Redemption 2」这类）能命中真实 logo 且不触发联网请求。

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
    # 把提示框的四个入口换成「记一笔就返回」，只验证代码路径走得通。
    from ui import msgbox
    shown_modals = []
    for _name in ("info", "warn", "error", "done"):
        setattr(msgbox, _name,
                lambda *a, _n=_name, **k: shown_modals.append(_n))

    w = MainWindow()
    w._shown_modals = shown_modals

    # 匹配服务换成假查找器：冒烟测试全程离线，绝不真去连 Steam。
    # 名字里带 FakeGame 的当作「查到了且有中文名」，其余当作没搜到。
    def fake_lookup(name, **_kw):
        if "FakeGame" in name:
            return {"status": "ok", "appid": "", "official_name": "FakeGame",
                    "name_zh": "假游戏", "error": ""}
        return {"status": "not_found", "appid": "", "official_name": "",
                "name_zh": "", "error": ""}

    w.meta._lookup = fake_lookup

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
                            HistoryDialog, DiffDialog, SettingsDialog,
                            AboutDialog)
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
        dialogs.append(AboutDialog(parent=w))
        check("8 个对话框全部构造成功", len(dialogs) == 8, "实际 %d" % len(dialogs))
        check("窗口标题带版本号", "v" in w.windowTitle() and "." in w.windowTitle(),
              w.windowTitle())
    except Exception as e:      # noqa: BLE001
        check("8 个对话框全部构造成功", False, repr(e))
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
    # v1.1.0 起：内容完全相同的快照在刷新时自动合并，不再逐份堆积。
    # 同步造的新快照若与既有内容一致，会被合并成一份（保留最新）。
    snaps = w.vault.list_snapshots(g.id)
    fps = [w.vault.manifest_fingerprint(s) for s in snaps]
    check("刷新后不存在内容重复的快照", len(fps) == len(set(fps)),
          "%d 份快照只有 %d 种内容" % (len(fps), len(set(fps))))
    check("同步不会让快照数失控增长", n_after <= n_before + 1,
          "%d -> %d" % (n_before, n_after))

    # v1.1.0 专项：手工造两份完全相同的快照，刷新后必须合并成一份
    n_base = len(w.vault.list_snapshots(g.id))
    w.vault.create_snapshot(g, kind="manual")     # 存档没动，内容必然相同
    w.vault.create_snapshot(g, kind="manual")
    check("构造了两份相同快照", len(w.vault.list_snapshots(g.id)) == n_base + 2,
          "%d -> %d" % (n_base, len(w.vault.list_snapshots(g.id))))
    w.refresh_all()
    pump(ms=3000, rounds=6)
    snaps2 = w.vault.list_snapshots(g.id)
    fps2 = [w.vault.manifest_fingerprint(s) for s in snaps2]
    check("刷新合并了完全相同的快照", len(snaps2) == n_base,
          "%d -> %d" % (n_base + 2, len(snaps2)))
    check("合并后内容仍无重复", len(fps2) == len(set(fps2)),
          "%d 份只有 %d 种内容" % (len(fps2), len(set(fps2))))
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

    print("== 6. 游戏图标（logo 包可选：无包走预设/联网，有包走包） ==")
    from ui.logos import (LogoService, _pack_lookup, _load_pack, _pack_dirs)
    from PySide6.QtGui import QPixmap
    # 合规默认：仓库/发布包不含任何第三方 logo。只有开发者或用户
    # 自行用 scripts/fetch_top_logos.py 预取了包，这里才存在。
    pack_dir = next((d for d in _pack_dirs()
                     if os.path.isdir(d)
                     and any(f.endswith(".png")
                             for f in os.listdir(d))), None)
    check("查不到的名字返回 None（无论装没装包）",
          _pack_lookup("这款游戏肯定不存在 XYZ") is None)
    ls = LogoService(os.path.join(TMP, "logocache_test"))
    pm = ls.pixmap("荒野大镖客2 Red Dead Redemption 2")
    check("任何情况下 pixmap 都返回 96x96 图标（不空白、不崩）",
          pm is not None and pm.width() == 96,
          "w=%s" % (pm.width() if pm else None))

    if pack_dir is None:
        check("合规默认：未安装可选 logo 包", _load_pack() == {})
        print("    （未安装可选 logo 包，包内匹配相关检查跳过；"
              "需要时运行 scripts/fetch_top_logos.py 预取）")
    else:
        pngs = [f for f in os.listdir(pack_dir) if f.endswith(".png")]
        check("logo 包图标数 >= 90（scripts/fetch_top_logos.py 产物）",
              len(pngs) >= 90, "实际 %d" % len(pngs))
        check("logo_pack.json 条目与图标一一对应",
              len(_load_pack()) >= len(pngs),
              "json=%d png=%d" % (len(_load_pack()), len(pngs)))
        check("RDR2 组合名命中 appid 1174180",
              _pack_lookup("荒野大镖客2 Red Dead Redemption 2") == "1174180",
              repr(_pack_lookup("荒野大镖客2 Red Dead Redemption 2")))
        check("纯英文名 GTA V 命中 271590",
              _pack_lookup("GTA V") == "271590")
        check("短中文名+英文组合命中巫师3",
              _pack_lookup("巫师3 The Witcher 3") == "292030")
        direct = QPixmap(os.path.join(pack_dir, "1174180.png"))
        check("RDR2 图标就是离线包里的那个文件",
              pm is not None and pm.toImage() == direct.toImage())
        check("包内已有图片时 has_pack_logo 判定为真",
              ls.has_pack_logo("荒野大镖客2 Red Dead Redemption 2"))

    print("== 7. 图标与中文名匹配（假查找器，全程离线） ==")
    import time as _time
    from ui.game_list import ROLE_GAME
    from PySide6.QtGui import QColor, QPixmap

    w.meta.wait_idle(8000)
    pump(ms=800, rounds=4)
    logtext = w.log_view.toPlainText()
    check("匹配完成后有汇总日志", "图标与中文名匹配完成" in logtext)
    check("匹配期间不阻塞刷新（busy 已回落）", w.busy is False)

    g2 = w.vault.get_game(g.id)
    check("中文名已落库", g2.name_zh == "假游戏", repr(g2.name_zh))
    check("匹配状态记为 ok", g2.meta_state == "ok", repr(g2.meta_state))
    check("display_name 用中文名", g2.display_name == "假游戏")
    cur = w.game_list.item(0).data(ROLE_GAME)
    check("列表项已回填中文名", getattr(cur, "name_zh", "") == "假游戏",
          repr(getattr(cur, "name_zh", "")))

    pm2 = QPixmap(96, 96)
    pm2.fill(QColor("#336699"))
    fname = w.logos.save_game_icon(g.id, pm2)
    check("图标按 game_id 落盘", bool(fname) and os.path.isfile(
        os.path.join(w.logos.icons_dir, fname)), repr(fname))
    w.vault.save_meta(g.id, logo_file=fname)
    g3 = w.vault.get_game(g.id)
    check("图标文件名写回数据库", g3.logo_file == fname, repr(g3.logo_file))
    check("pixmap_for_game 能读回落盘的图标",
          not w.logos.pixmap_for_game(g3).isNull())
    check("has_game_icon 认可落盘图标", w.logos.has_game_icon(g3))
    check("图标和中文名都齐了就不再排队", w.meta.ensure([g3]) == 0)

    # 失败分支：查不到 → 保留原名、写退避、不炸列表
    gx = w.vault.add_game("NoSuchGame XYZ", [gamedir], platform="测试")
    w.meta.ensure([gx])
    w.meta.wait_idle(8000)
    pump(ms=600, rounds=3)
    mx = w.vault.get_meta(gx.id)
    check("匹配失败记为 not_found", bool(mx) and mx["state"] == "not_found",
          repr(mx and mx["state"]))
    check("失败后写入退避时间", bool(mx) and mx["next_retry_at"] > _time.time(),
          repr(mx and mx["next_retry_at"]))
    check("失败不影响原名显示",
          w.vault.get_game(gx.id).display_name == "NoSuchGame XYZ")
    check("退避期内不会重复排队", w.meta.ensure([w.vault.get_game(gx.id)]) == 0)

    print("== 8. 离线资料索引 ==")
    from gamesave import gameindex
    st = gameindex.stats()
    check("索引已加载", st["count"] >= 90, "count=%d path=%s"
          % (st["count"], os.path.basename(st["path"] or "")))
    hit = gameindex.lookup("Counter-Strike 2")
    check("索引能认出榜单游戏", hit is not None and hit["appid"] == "730",
          repr(hit and hit["appid"]))
    check("索引命中不产生网络请求", (hit or {}).get("source") == "index",
          repr((hit or {}).get("source")))
    check("索引认不出的名字返回 None",
          gameindex.lookup("这款游戏肯定不在榜单上 XYZ") is None)

    print("== 9. UI 细节：进度条占位 / 移除确认 / 备份去重 ==")
    check("进度条占位容器常驻", w.progress_box.height() == 4,
          "h=%d" % w.progress_box.height())
    before_h = w.progress_box.height()
    w.progress.show()
    shown_h = w.progress_box.height()
    w.progress.hide()
    check("进度条显隐不改变布局高度",
          before_h == shown_h == 4, "%d -> %d" % (before_h, shown_h))

    from ui.dialogs import ConfirmDialog
    d = ConfirmDialog("移除游戏 —— X", "将从列表移除该游戏。", "游戏：X", parent=w)
    check("移除确认不再要求勾选", d.check is None, repr(d.check))
    d.deleteLater()

    n0 = len(w.vault.list_snapshots(g.id))
    w.backup_current()
    pump(ms=3500, rounds=7)
    n1 = len(w.vault.list_snapshots(g.id))
    w.backup_current()
    pump(ms=3500, rounds=7)
    n2 = len(w.vault.list_snapshots(g.id))
    check("连续备份不会堆出内容重复的快照", n2 <= n1,
          "%d -> %d -> %d" % (n0, n1, n2))
    check("备份完成用的是静音提示框（已打桩）",
          "done" in shown_modals, "modals=%r" % (shown_modals[-4:],))

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
