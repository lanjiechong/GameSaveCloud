# -*- coding: utf-8 -*-
"""命令行版入口。

`run.py --cli xxx` 里的 `--cli` 是为了让同一个脚本既能开界面又能跑命令。
但打包成 exe 之后，计划任务的命令行写成
    GameSaveCloud-CLI.exe --cli sync
很别扭。这个文件就是为打包出来的命令行版准备的。

用法：
    GameSaveCloud-CLI.exe status    :: 各游戏同步状态
    GameSaveCloud-CLI.exe scan      :: 扫描本机存档目录
    GameSaveCloud-CLI.exe backup    :: 立即备份全部
    GameSaveCloud-CLI.exe sync      :: 同步全部（未配云端时等同 backup）
    GameSaveCloud-CLI.exe --version :: 显示版本号

退出码：0 成功 / 1 用法或参数错误 / 2 存在未处理的冲突。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from version import APP_NAME, VERSION  # noqa: E402
from run import _cli  # noqa: E402

if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--version" in argv or "-V" in argv:
        print("%s %s" % (APP_NAME, VERSION))
        sys.exit(0)
    sys.exit(_cli(argv) or 0)
