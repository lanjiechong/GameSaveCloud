# -*- coding: utf-8 -*-
"""
视觉主题：浅色、克制、信息密度优先。

状态色语义（遵循国内习惯，绿=正常，红=异常，橙=待处理）：
    已同步    green
    待上传    amber    本地有新进度
    待下载    blue     云端有新进度
    冲突      red      需要人决策
    未同步    gray
    错误      darkred
"""

PALETTE = {
    "synced":      "#1a7f37",
    "synced_bg":   "#eaf6ec",
    "local":       "#b25e09",
    "local_bg":    "#fdf3e3",
    "remote":      "#0969da",
    "remote_bg":   "#e8f1fd",
    "conflict":    "#c9372c",
    "conflict_bg": "#fdecea",
    "unknown":     "#6b7280",
    "unknown_bg":  "#f1f2f4",
    "error":       "#8b1a12",
    "error_bg":    "#fbe4e1",
    "bg":          "#f6f7f9",
    "panel":       "#ffffff",
    "border":      "#e2e5ea",
    "text":        "#1f2328",
    "muted":       "#656d76",
    "accent":      "#0969da",
    "danger":      "#c9372c",
}

STATE_LABEL = {
    "synced":  "已同步",
    "local":   "待上传",
    "remote":  "待下载",
    "conflict": "有冲突",
    "unknown": "未同步",
    "error":   "异常",
}

# 本地备份模式（未配置云端）下的同状态文案。
# 状态机复用同一套常量，改的只是「给人看的那几个字」。
LOCAL_LABEL = {
    "synced":  "已备份",
    "local":   "待备份",
    "unknown": "未备份",
    "error":   "异常",
}

STYLESHEET = """
QWidget { font-family: "Microsoft YaHei UI", "PingFang SC", "Noto Sans CJK SC", sans-serif;
          font-size: 13px; color: #1f2328; }
QMainWindow, QDialog { background: #f6f7f9; }
QFrame#Panel, QListWidget, QTableWidget, QTreeWidget, QPlainTextEdit, QTextEdit {
    background: #ffffff; border: 1px solid #e2e5ea; border-radius: 6px; }
QPushButton { background: #ffffff; border: 1px solid #d0d7de; border-radius: 6px;
              padding: 5px 14px; min-height: 20px; }
QPushButton:hover { background: #f3f4f6; }
QPushButton:pressed { background: #ebecf0; }
QPushButton:disabled { color: #9aa0a6; background: #f6f7f9; }
QPushButton#Primary { background: #0969da; color: #ffffff; border-color: #0969da; }
QPushButton#Primary:hover { background: #0860c4; }
QPushButton#Primary:disabled { background: #a8c7ee; border-color: #a8c7ee; }
QPushButton#Danger { background: #ffffff; color: #c9372c; border-color: #e0a9a3; }
QPushButton#Danger:hover { background: #fdecea; }
QListWidget { outline: none; }
QListWidget::item { padding: 8px 10px; border-radius: 6px; margin: 2px 4px; }
QListWidget::item:selected { background: #e8f1fd; color: #1f2328; }
QHeaderView::section { background: #f6f7f9; border: none; border-bottom: 1px solid #e2e5ea;
                       padding: 6px 8px; font-weight: 600; }
QTabWidget::pane { border: 1px solid #e2e5ea; border-radius: 6px; background: #ffffff; top: -1px; }
QTabBar::tab { background: transparent; padding: 7px 16px; border: none; }
QTabBar::tab:selected { color: #0969da; border-bottom: 2px solid #0969da; }
QGroupBox { border: 1px solid #e2e5ea; border-radius: 6px; margin-top: 10px;
            padding-top: 8px; background: #ffffff; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #656d76; }
QLabel#Muted { color: #656d76; }
QLabel#H1 { font-size: 17px; font-weight: 700; }
QLabel#H2 { font-size: 14px; font-weight: 600; }
QProgressBar { border: 1px solid #e2e5ea; border-radius: 4px; background: #f6f7f9;
               text-align: center; height: 16px; }
QProgressBar::chunk { background: #0969da; border-radius: 3px; }
QLineEdit, QSpinBox, QComboBox, QCheckBox { background: #ffffff; border: 1px solid #d0d7de;
              border-radius: 6px; padding: 4px 8px; min-height: 22px; }
QPlainTextEdit { font-family: "Cascadia Mono", Consolas, monospace; font-size: 12px; }
QScrollBar:vertical { background: transparent; width: 10px; }
QScrollBar::handle:vertical { background: #d0d7de; border-radius: 5px; min-height: 30px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
QToolTip { background: #1f2328; color: #ffffff; border: none; padding: 4px 8px; }
"""


def state_color(state: str) -> str:
    return PALETTE.get(state, PALETTE["unknown"])


def state_bg(state: str) -> str:
    return PALETTE.get(state + "_bg", PALETTE["unknown_bg"])


def state_label(state: str, cloud_on: bool = True) -> str:
    """同一状态在两种模式下的文案。"""
    if not cloud_on:
        return LOCAL_LABEL.get(state, STATE_LABEL.get(state, state))
    return STATE_LABEL.get(state, state)
