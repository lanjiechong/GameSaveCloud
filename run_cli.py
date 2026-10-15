# -*- coding: utf-8 -*-
"""命令行版入口。

`run.py --cli xxx` 里的 `--cli` 是为了让同一个脚本既能开界面又能跑命令。
但打包成 exe 之后，计划任务的命令行写成
    GameSaveCloud-CLI.exe --cli sync
很别扭。这个文件就是为打包出来的命令行版准备的：
    GameSaveCloud-CLI.exe sync
    GameSaveCloud-CLI.exe status
    GameSaveCloud-CLI.exe backup

退出码：0 成功 / 1 用法或参数错误 / 2 存在未处理的冲突。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from run import _cli  # noqa: E402

if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]) or 0)
