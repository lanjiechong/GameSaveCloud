# -*- coding: utf-8 -*-
"""
开机自启（仅当前用户，不需要管理员权限）。

写注册表 HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run，
登录后以 ``--minimized`` 启动，主窗口不弹出、只驻留托盘。

用标准库 winreg 而非 QSettings：引擎/CLI 不依赖 Qt 也能读这个状态。

打包后命令是 ``"C:\\...\\GameSaveCloud.exe" --minimized``；
源码运行时优先用同目录的 pythonw.exe（无控制台黑窗）：
``"pythonw.exe" "C:\\...\\run.py" --minimized``。
"""
import os
import sys

VALUE_NAME = "GameSaveCloud"
_RUN_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

try:
    import winreg  # 仅 Windows 存在
except ImportError:
    winreg = None


def launch_command() -> str:
    """开机时要执行的命令行。"""
    exe_dir = os.path.dirname(sys.executable)
    if getattr(sys, "frozen", False):
        return '"%s" --minimized' % sys.executable
    # 源码运行：优先 pythonw.exe，避免开机弹出控制台窗口
    pyw = os.path.join(exe_dir, "pythonw.exe")
    python = pyw if os.path.isfile(pyw) else sys.executable
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return '"%s" "%s" --minimized' % (python, os.path.join(root, "run.py"))


def is_enabled() -> bool:
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_SUBKEY) as key:
            winreg.QueryValueEx(key, VALUE_NAME)
        return True
    except OSError:
        return False


def set_enabled(on: bool) -> bool:
    """开启/关闭开机自启。返回设置后的实际状态。

    非 Windows 平台直接返回 False（不支持）。
    """
    if winreg is None:
        return False
    if on:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _RUN_SUBKEY, 0,
                                winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ,
                              launch_command())
    else:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_SUBKEY, 0,
                                winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, VALUE_NAME)
        except FileNotFoundError:
            pass
    return is_enabled()
