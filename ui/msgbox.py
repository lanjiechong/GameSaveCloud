# -*- coding: utf-8 -*-
"""统一的提示对话框。

为什么不用 QMessageBox：
    Windows 上它会触发系统提示音 —— 备份完成、同步完成这种「好事」
    也照样响一声。用户明确要求去掉，而这个声音由平台主题控制，
    Python 层没有开关。所以改成纯 Qt 自绘的 QDialog：不碰任何系统
    声音接口，配色也跟随应用主题，不会突然弹出一个系统灰框。

接口刻意与 QMessageBox 的静态方法对齐（parent, title, text），
调用点只需要换前缀，不需要改参数顺序。
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPushButton,
                               QVBoxLayout)

# 圆形徽章：文字 / 底色
_ICONS = {
    "info": ("i", "#2f6fb3"),
    "warn": ("!", "#b7791f"),
    "error": ("×", "#c0392b"),
    "done": ("✓", "#2f855a"),
}


class MessageDialog(QDialog):
    """一条提示 + 一个「确定」。默认按钮带焦点，回车即可关闭。"""

    def __init__(self, title, text, kind="info", detail="", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(420)
        if parent is not None and parent.styleSheet():
            # 跟随主窗口的浅色主题，别让它自己变成系统灰
            self.setStyleSheet(parent.styleSheet())

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(12)

        row = QHBoxLayout()
        row.setSpacing(12)
        mark, color = _ICONS.get(kind, _ICONS["info"])
        badge = QLabel(mark)
        badge.setFixedSize(30, 30)
        badge.setAlignment(Qt.AlignCenter)
        badge.setStyleSheet(
            "background:%s; color:#ffffff; border-radius:15px;"
            "font-size:16px; font-weight:700;" % color)
        row.addWidget(badge, 0, Qt.AlignTop)

        body = QLabel(text or "")
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        row.addWidget(body, 1)
        lay.addLayout(row)

        btn = QPushButton("确定")
        btn.setObjectName("Primary")
        btn.setDefault(True)
        btn.setAutoDefault(True)
        btn.clicked.connect(self.accept)
        bb = QHBoxLayout()
        bb.addStretch(1)
        bb.addWidget(btn)
        lay.addLayout(bb)
        self._btn = btn

    def showEvent(self, ev):
        super().showEvent(ev)
        # 让回车直接关掉，不用先 Tab 到按钮
        self._btn.setFocus()


def info(parent, title, text, **kw):
    return MessageDialog(title, text, "info", parent=parent).exec()


def warn(parent, title, text, **kw):
    return MessageDialog(title, text, "warn", parent=parent).exec()


def error(parent, title, text, **kw):
    return MessageDialog(title, text, "error", parent=parent).exec()


def done(parent, title, text, **kw):
    """操作成功。与 info 同形，只是换成绿色对勾。"""
    return MessageDialog(title, text, "done", parent=parent).exec()
