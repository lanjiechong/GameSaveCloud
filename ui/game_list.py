# -*- coding: utf-8 -*-
"""
游戏列表的自绘委托。

为什么不用「item + setItemWidget(QWidget)」：
    每个条目塞一个 QWidget 意味着 N 个 QWidget + N 套 layout，
    窗口缩放时全部重新布局；而且 QWidget 拿不到原生的 hover/selected
    状态，选中态要靠手动配色，容易和系统主题打架。

改用 QStyledItemDelegate 自绘，一行 paint 画完，代价与条目数无关：
    - 固定行高与内边距，缩放窗口不会挤在一起
    - 图标 + 文本严格两栏，间距是常量
    - 文本过长用 QFontMetrics.elidedText 截断，悬浮显示全称
    - 悬停 / 选中 / 普通三态各有底色，选中再加左侧强调条

条目数据（由 MainWindow 填充）：
    Qt.UserRole      Game
    Qt.UserRole + 1  GameStatus
    Qt.UserRole + 2  bool  cloud_on（决定「待上传」还是「待备份」）
"""
import time

from PySide6.QtCore import QEvent, QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QToolTip

from gamesave.chunker import human_size
from .theme import PALETTE, state_color, state_label

ROLE_GAME = Qt.UserRole
ROLE_STATUS = Qt.UserRole + 1
ROLE_CLOUD = Qt.UserRole + 2

ROW_H = 64            # 行高
ICON = 44             # 图标边长
GAP = 12              # 图标与文字之间的固定间距
MARGIN_X = 10         # 左右内边距
DOT = 8               # 状态圆点直径
DOT_GAP = 14          # 文字区与圆点的间距
LINE_GAP = 3          # 两行文字之间的行距
INSET = 3             # 底色矩形相对条目矩形的内缩
RADIUS = 8            # 底色圆角


class GameItemDelegate(QStyledItemDelegate):
    """画一行游戏：图标 | 名称/副标题 | 状态点。"""

    def __init__(self, logos=None, parent=None):
        super().__init__(parent)
        self.logos = logos
        f = QFont()
        f.setPixelSize(13)
        f.setWeight(QFont.DemiBold)
        self._f_name = f
        f2 = QFont()
        f2.setPixelSize(11)
        self._f_sub = f2
        self._fm_name = QFontMetrics(self._f_name)
        self._fm_sub = QFontMetrics(self._f_sub)

    # ==========================================================
    # 尺寸
    # ==========================================================
    def sizeHint(self, option, index):
        # 高度写死，宽度跟随视图。写死高度是「不挤在一起」的关键：
        # 两行文字 + 图标需要 64px，比默认的 ~20px 宽松得多。
        w = option.rect.width()
        return QSize(max(w, 240), ROW_H)

    # ==========================================================
    # 绘制
    # ==========================================================
    def paint(self, painter, option, index):
        game = index.data(ROLE_GAME)
        st = index.data(ROLE_STATUS)
        cloud_on = bool(index.data(ROLE_CLOUD))
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        try:
            self._paint_background(painter, option)
            if game is None:
                return
            self._paint_icon(painter, option, game)
            self._paint_texts(painter, option, game, st, cloud_on)
            self._paint_dot(painter, option, st)
        finally:
            painter.restore()

    # ---------- 背景：普通 / 悬停 / 选中 ----------
    def _paint_background(self, painter, option):
        r = option.rect.adjusted(INSET, INSET, -INSET, -INSET)
        state = option.state
        if state & QStyle.State_Selected:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(PALETTE["remote_bg"]))
            painter.drawRoundedRect(r, RADIUS, RADIUS)
            # 左侧强调条：细一条，明确标出「当前选中的是这个」
            bar = QRect(r.left() + 2, r.top() + 8, 3, r.height() - 16)
            painter.setBrush(QColor(PALETTE["accent"]))
            painter.drawRoundedRect(bar, 1, 1)
        elif state & QStyle.State_MouseOver:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor("#f0f1f4"))
            painter.drawRoundedRect(r, RADIUS, RADIUS)

    # ---------- 图标 ----------
    def _paint_icon(self, painter, option, game):
        top = option.rect.top() + (ROW_H - ICON) // 2
        target = QRect(option.rect.left() + MARGIN_X + 4, top, ICON, ICON)
        pm = None
        if self.logos is not None:
            try:
                # 优先用匹配阶段按 game_id 落盘的图标；
                # 没有就走「缓存 → 离线包 → 内置预设 → 占位」链路。
                pm = self.logos.pixmap_for_game(game)
            except Exception:
                pm = None
        if pm is None or pm.isNull():
            # 理论上 pixmap_for_game 永远不返回 None；真出了问题
            # 画个空框，也别让整行消失。
            painter.setPen(QPen(QColor(PALETTE["border"]), 1))
            painter.setBrush(QColor(PALETTE["panel"]))
            painter.drawRoundedRect(target, 10, 10)
            return
        painter.drawPixmap(target, pm)

    # ---------- 文本两栏 ----------
    def _paint_texts(self, painter, option, game, st, cloud_on):
        tx = option.rect.left() + MARGIN_X + 4 + ICON + GAP
        right = option.rect.right() - MARGIN_X - DOT - DOT_GAP
        avail = max(right - tx, 40)

        h_name = self._fm_name.height()
        h_sub = self._fm_sub.height()
        top = option.rect.top() + (ROW_H - (h_name + h_sub + LINE_GAP)) // 2

        name_rect = QRect(tx, top, avail, h_name)
        sub_rect = QRect(tx, top + h_name + LINE_GAP, avail, h_sub)

        # 名称：优先中文名；没匹配到就用原名，两者都不缺时不会显示空白
        name = getattr(game, "display_name", "") or getattr(game, "name", "")
        painter.setFont(self._f_name)
        painter.setPen(QColor(PALETTE["text"]))
        painter.drawText(name_rect,
                         Qt.AlignLeft | Qt.AlignVCenter | Qt.TextSingleLine,
                         self._fm_name.elidedText(name, Qt.ElideRight, avail))

        # 副标题：状态 · 体积 · 最后同步
        if st is not None:
            label = state_label(st.state, cloud_on)
            last = time.strftime("%m-%d %H:%M", time.localtime(st.last_sync_at)) \
                if st.last_sync_at else "从未"
            sub = "%s · %s · %s" % (label, _human(st.local_size), last)
        else:
            sub = ""
        painter.setFont(self._f_sub)
        painter.setPen(QColor(PALETTE["muted"]))
        painter.drawText(sub_rect,
                         Qt.AlignLeft | Qt.AlignVCenter | Qt.TextSingleLine,
                         self._fm_sub.elidedText(sub, Qt.ElideRight, avail))

    # ---------- 状态圆点 ----------
    def _paint_dot(self, painter, option, st):
        color = state_color(st.state) if st is not None else PALETTE["unknown"]
        cy = option.rect.center().y()
        cx = option.rect.right() - MARGIN_X - DOT // 2
        dot = QRect(cx - DOT // 2, cy - DOT // 2, DOT, DOT)
        # 外圈：让彩色点在白底/浅底上都分明
        painter.setPen(QPen(QColor(255, 255, 255, 200), 1))
        painter.setBrush(QColor(color))
        painter.drawEllipse(dot)

    # ==========================================================
    # 悬浮提示：完整名称 + 全部副标题信息
    # ==========================================================
    def helpEvent(self, event, view, option, index):
        if event.type() == QEvent.ToolTip:
            game = index.data(ROLE_GAME)
            st = index.data(ROLE_STATUS)
            cloud_on = bool(index.data(ROLE_CLOUD))
            if game is not None:
                title = getattr(game, "display_name", "") or game.name
                lines = ["<b>%s</b>" % _esc(title)]
                # 列表上显示的是中文名，原名放这里，方便对照存档目录
                if getattr(game, "name_zh", "") and game.name_zh != game.name:
                    lines.append("原名：%s" % _esc(game.name))
                if game.paths:
                    lines.append("存档目录：%s" % _esc("、".join(game.paths)))
                if st is not None:
                    last = time.strftime("%Y-%m-%d %H:%M:%S",
                                         time.localtime(st.last_sync_at)) \
                        if st.last_sync_at else "从未"
                    lines.append("状态：%s · 本地 %s · 最后同步 %s"
                                 % (_esc(state_label(st.state, cloud_on)),
                                    _human(st.local_size), last))
                    if st.message:
                        lines.append(_esc(st.message))
                QToolTip.showText(event.globalPos(), "<br>".join(lines), view)
                return True
        return super().helpEvent(event, view, option, index)


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _human(n) -> str:
    """与界面其他位置保持同一套体积格式。"""
    return human_size(n or 0)
