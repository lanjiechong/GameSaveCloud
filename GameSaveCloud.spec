# -*- coding: utf-8 -*-
"""
GameSaveCloud 打包配置（PyInstaller 6.x）。

用法：
    pyinstaller GameSaveCloud.spec --noconfirm

产出（在 dist/ 下）：
    GameSaveCloud.exe       单文件、无控制台窗口 —— 双击即启动图形界面
    GameSaveCloud-CLI.exe   单文件、带控制台   —— 供计划任务 / 脚本调用

两个 exe 共享同一份 Analysis/PYZ（依赖只分析一次），
差异仅在最后的 EXE 层：console 开或关。
"""
import os

# 让 spec 在任意工作目录下都能找到项目根
HERE = os.path.dirname(os.path.abspath(SPEC))

block_cipher = None

# 用不到的 Qt 模块。排除它们能显著缩小体积，
# 但每一项都必须是「确定没用到」的：QtWidgets 运行时会拉
# QtGui/QtCore/QtNetwork/QtOpenGL/QtPrintSupport，这些不能排。
EXCLUDES = [
    # —— 大型且确定未使用的模块 ——
    "QtWebEngineCore", "QtWebEngineWidgets", "QtWebEngineQuick",
    "QtWebChannel", "QtWebSockets",
    "Qt3DCore", "Qt3DRender", "Qt3DInput", "Qt3DLogic",
    "Qt3DAnimation", "Qt3DExtras",
    "QtCharts", "QtDataVisualization",
    "QtQuick", "QtQuick3D", "QtQuickControls2", "QtQuickWidgets",
    "QtQml", "QtQmlModels",
    "QtMultimedia", "QtMultimediaWidgets",
    # —— 硬件 / 定位类，桌面同步工具用不到 ——
    "QtBluetooth", "QtNfc", "QtSerialPort", "QtSerialBus",
    "QtLocation", "QtPositioning", "QtSensors",
    # —— 工具类 ——
    "QtTest", "QtDesigner", "QtHelp", "QtUiTools", "QtDBus",
]

a = Analysis(
    [os.path.join(HERE, "run.py")],
    pathex=[HERE],
    binaries=[],
    # 应用图标（窗口图标 + 托盘图标）。打包后解到 _MEIPASS/ui/assets/，
    # 与开发时的 ui/assets/ 相对位置一致，_asset() 两条路径都能命中。
    datas=[(os.path.join(HERE, "ui", "assets"), os.path.join("ui", "assets"))],
    # zstandard 在 chunker.py 里是 try-import 的可选加速，
    # 显式声明一下，避免 PyInstaller 因为它是可选的而漏掉。
    hiddenimports=["zstandard"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# 命令行版单独做一次 Analysis：入口是 run_cli.py，
# 它 import run 再直接调 _cli()，所以命令行参数不需要 --cli 前缀。
a_cli = Analysis(
    [os.path.join(HERE, "run_cli.py")],
    pathex=[HERE],
    binaries=[],
    datas=[(os.path.join(HERE, "ui", "assets"), os.path.join("ui", "assets"))],
    hiddenimports=["zstandard"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
pyz_cli = PYZ(a_cli.pure, a_cli.zipped_data, cipher=block_cipher)

# 图形界面版：无控制台窗口，双击就是一个普通 Windows 应用
exe_gui = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="GameSaveCloud",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # exe 自身的图标（资源管理器里显示的那个）。
    # 窗口/托盘图标走的是 ui/assets，两者互不依赖。
    icon=os.path.join(HERE, "ui", "assets", "app_logo.ico"),
)

# 命令行版：保留控制台，供 `GameSaveCloud-CLI.exe sync` 这类无人值守调用
exe_cli = EXE(
    pyz_cli,
    a_cli.scripts,
    a_cli.binaries,
    a_cli.zipfiles,
    a_cli.datas,
    [],
    name="GameSaveCloud-CLI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(HERE, "ui", "assets", "app_logo.ico"),
)
