# -*- coding: utf-8 -*-
"""
GameSaveCloud 启动入口。

用法：
    python run.py                 # 启动图形界面
    python run.py --cli status    # 命令行查看各游戏同步状态
    python run.py --cli scan      # 命令行扫描本机存档目录
    python run.py --cli sync      # 命令行同步全部（冲突时退出并提示）
    python run.py --cli backup    # 命令行立即备份全部
    python run.py --version       # 显示版本号
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from version import APP_NAME, VERSION


def main():
    args = sys.argv[1:]
    if "--version" in args or "-V" in args:
        print("%s %s" % (APP_NAME, VERSION))
        return 0
    if "--cli" in args:
        return _cli(args[args.index("--cli") + 1:])

    from PySide6.QtWidgets import QApplication
    from ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(VERSION)
    app.setQuitOnLastWindowClosed(False)
    w = MainWindow()
    w.show()
    return app.exec()


# ==================================================================
# 命令行模式：无头环境 / 脚本化 / 调试
# ==================================================================
def _cli(argv):
    if not argv:
        print(__doc__)
        return 0
    cmd = argv[0]

    from gamesave.config import Config
    from gamesave.vault import Vault
    from gamesave.remote import LocalFolderBackend, WebDAVBackend
    from gamesave.sync import SyncEngine
    from gamesave import scanner

    cfg = Config()
    vault = Vault(cfg.vault_root)
    # 没配云端就不建后端对象，也不创建那个目录
    cloud_on = cfg.cloud_enabled
    remote = None
    if cloud_on:
        r = cfg.remote_conf
        if r.get("type") == "webdav" and r.get("url"):
            remote = WebDAVBackend(r["url"], r.get("user", ""), r.get("password", ""))
        else:
            remote = LocalFolderBackend(r.get("path"))
    eng = SyncEngine(vault, remote, log=lambda m: print("  " + m),
                     cloud_on=cloud_on)
    if not cloud_on:
        print("（未配置云端，当前为本地备份模式）")

    def prune(g):
        keep = int(cfg.safety.get("keep_snapshots", 30) or 0)
        if keep <= 0:
            return
        n = vault.prune_snapshots(g.id, keep)
        nr = 0
        if remote is not None:
            ss = vault.get_sync_state(g.id) or {}
            nr = remote.prune_snapshots(g.id, keep,
                                        protect={ss.get("base_snapshot_id")})
        if n or nr:
            print("    已裁剪旧快照：本地 %d / 云端 %d（保留最近 %d 版）"
                  % (n, nr, keep))

    def backup_all():
        for g in vault.list_games():
            s = vault.create_snapshot(g, kind="auto", note="CLI 备份")
            print("%-28s %d 文件 %s" % (g.name, len(s.files), _h(s.total_size)))
            prune(g)
        vault.gc()

    if cmd == "status":
        far = "云端" if cloud_on else "最新备份"
        for g in vault.list_games():
            st = eng.status(g)
            print("%-28s %-8s 本地 %-10s %s %-10s 快照 %-3d %s"
                  % (g.name, _label(st.state, cloud_on), _h(st.local_size),
                     far, _h(st.remote_size), st.snapshot_count, st.message))
        return 0

    if cmd == "scan":
        known = [p for g in vault.list_games() for p in g.paths]
        for fg in scanner.scan(known_paths=known):
            print("%-30s %-8s %3d 文件 %-10s %s"
                  % (fg.name[:30], fg.source, fg.files, _h(fg.size),
                     " / ".join(fg.paths)))
        return 0

    if cmd == "backup":
        backup_all()
        return 0

    if cmd == "sync":
        if not cloud_on:
            # 本地模式下「同步」就是「全部备份到本地保险库」
            print("未配置云端，转为本地备份：")
            backup_all()
            return 0
        conflicts = []
        for g in vault.list_games():
            plan = eng.plan(g)
            if plan.error:
                print("! %s: %s" % (g.name, plan.error))
                continue
            if plan.has_conflict:
                conflicts.append((g, plan))
                print("! %s: %d 个冲突" % (g.name, len(plan.conflicts)))
                continue
            res = eng.execute(g, plan, {})
            print("%-28s %s  上传 %d / 下载 %d chunk"
                  % (g.name, plan.summary(), res["uploaded"], res["downloaded"]))
            prune(g)
        vault.gc()
        if conflicts:
            print("\n有 %d 款游戏存在冲突，请在图形界面中处理。" % len(conflicts))
            return 2
        return 0

    print("未知命令: %s" % cmd)
    print(__doc__)
    return 1


# 命令行不引 Qt（theme.py 依赖 PySide6），所以在这里放一份小号标签表。
_STATE_LABEL = {"synced": "已同步", "local": "待上传", "remote": "待下载",
                "conflict": "有冲突", "unknown": "未同步", "error": "异常"}
_STATE_LABEL_LOCAL = {"synced": "已备份", "local": "待备份", "remote": "待下载",
                      "conflict": "有冲突", "unknown": "未备份", "error": "异常"}


def _label(state, cloud_on):
    return (_STATE_LABEL_LOCAL if not cloud_on else _STATE_LABEL).get(state, state)


def _h(n):
    from gamesave import chunker
    return chunker.human_size(n)


if __name__ == "__main__":
    sys.exit(main())
