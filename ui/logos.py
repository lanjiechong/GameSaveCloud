# -*- coding: utf-8 -*-
"""
游戏图标（logo）加载服务。

三级保障，任何一层失败都不会出现空白、破图或布局错位：

    1. 已下载缓存    磁盘缓存命中 → 直接返回，离线也能用
    2. 内置预设      常见热门游戏有预置配色与代号，**立刻可画**，
                     不依赖网络，不等请求
    3. 联网检索      其余游戏走 Steam 公开接口找真实 logo
    4. 占位兜底      全部失败时画一个「首字母色块」，
                     尺寸、圆角、位置与真实图标完全一致

图标来源（联网那一层）：
    Steam 商店搜索 API（免密钥）
        https://store.steampowered.com/api/storesearch/?term=<游戏名>&cc=cn&l=schinese
     返回 appid 后取图片：
        https://cdn.akamai.steamstatic.com/steam/apps/<appid>/logo.png     ← 透明底，首选
        https://cdn.akamai.steamstatic.com/steam/apps/<appid>/header.jpg   ← 兜底

为什么用 Steam：游戏覆盖面最广、免申请 key、有公开 CDN。
风险是名称匹配可能对不上，所以加了一道相似度校验 ——
对不上就当作没找到，回落到预设/占位，绝不显示一张来路不明的图。

所有网络 IO 都在工作线程里跑，主线程只收信号。
"""
import hashlib
import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
import zlib

from PySide6.QtCore import QObject, QThreadPool, Qt, Signal
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QImage, QLinearGradient,
                           QPainter, QPainterPath, QPen, QPixmap)

TILE = 96                      # 图标绘制尺寸（超采样，显示时缩到 40 左右更清晰）
UA = "Mozilla/5.0 (compatible; GameSaveCloud/1.0)"
TIMEOUT = 12

# 常见热门游戏预置：名字 → (主色, 短代号)
# 这些是「内置」的：断网也能立刻有一个像样的图标，
# 不会出现空白占位。联网拿到真实 logo 后会替换掉它。
BUILTIN = {
    "艾尔登法环":        ("#c8963e", "ER"),
    "黑神话悟空":        ("#b5533c", "WU"),
    "黑神话：悟空":       ("#b5533c", "WU"),
    "赛博朋克2077":      ("#f0b429", "CP"),
    "巫师3狂猎":         ("#7a9e3f", "W3"),
    "巫师3：狂猎":       ("#7a9e3f", "W3"),
    "荒野大镖客2":       ("#c0392b", "RD2"),
    "荒野大镖客救赎":     ("#c0392b", "RDR"),
    "侠盗猎车手5":       ("#8e9a3a", "GTA"),
    "侠盗猎车手Online":  ("#8e9a3a", "GTA"),
    "只狼影逝二度":       ("#5b6b8c", "SEK"),
    "战神":              ("#9b59b6", "GOW"),
    "最终幻想14":        ("#3a6ea5", "FF14"),
    "最终幻想7重制版":    ("#3a6ea5", "FF7"),
    "怪物猎人世界":       ("#8c5a3c", "MHW"),
    "怪物猎人荒野":       ("#8c5a3c", "MHW"),
    "永劫无间":          ("#2e8b8b", "NAR"),
    "原神":              ("#4a90d9", "GNS"),
    "崩坏星穹铁道":       ("#5a7fd6", "SR"),
    "崩坏：星穹铁道":     ("#5a7fd6", "SR"),
    "绝区零":            ("#e07b39", "ZZZ"),
    "英雄联盟":          ("#2b6cb0", "LOL"),
    "反恐精英2":         ("#d69e2e", "CS2"),
    "无畏契约":          ("#c0392b", "VAL"),
    "Apex英雄":          ("#c05621", "APX"),
    "绝地求生":          ("#b7791f", "PUB"),
    "我的世界":          ("#6b9e3f", "MC"),
    "泰拉瑞亚":          ("#7a9e3f", "TRA"),
    "星露谷物语":        ("#9c7a3f", "SDV"),
    "哈迪斯2":           ("#c05621", "HD2"),
    "空洞骑士":          ("#4a6fa5", "HK"),
    "死亡细胞":          ("#8e44ad", "DC"),
    "博德之门3":         ("#b7791f", "BG3"),
    "文明6":             ("#3f6e9e", "CIV"),
    "全面战争三国":       ("#a93226", "TWT"),
    "鬼谷八荒":          ("#7d6608", "GG"),
    "戴森球计划":        ("#1f618d", "DSP"),
    "英灵神殿":          ("#6e8b3d", "VAL"),
    "幻兽帕鲁":          ("#2e86c1", "PAL"),
    "尼尔机械纪元":       ("#7f8c8d", "NIE"),
    "仁王2":             ("#922b21", "NIO"),
    "黑暗之魂3":         ("#566573", "DS3"),
    "血源诅咒":          ("#7b241c", "BB"),
    "对马岛之魂":        ("#1a5276", "GOT"),
    "最后生还者":        ("#6e2c00", "TLO"),
    "双人成行":          ("#e67e22", "ITA"),
    "糖豆人":            ("#d6336c", "FBG"),
    "逃离塔科夫":        ("#7d6608", "EFT"),
    "腐蚀":              ("#a04000", "RUS"),
    "方舟生存进化":       ("#b9770e", "ARK"),
    "森林之子":          ("#1e8449", "SON"),
    "盗贼之海":          ("#117864", "SOT"),
    "塞尔达传说":        ("#27ae60", "ZEL"),
    "斯普拉遁3":         ("#e74c3c", "SPL"),
    "宝可梦":            ("#f1c40f", "PKM"),
    "火焰纹章":          ("#8e44ad", "FE"),
    "集合啦动物森友会":    ("#16a085", "ACNH"),
    "星之卡比":          ("#e91e63", "KBY"),
    "红色警戒2":         ("#c0392b", "RA2"),
    "星际争霸2":         ("#1f618d", "SC2"),
    "魔兽世界":          ("#1a5276", "WOW"),
    "暗黑破坏神4":       ("#7b241c", "D4"),
    "使命召唤":          ("#424949", "COD"),
    "战地风云2042":      ("#515a5a", "BF"),
    "街霸6":             ("#2471a3", "SF6"),
    "铁拳8":             ("#922b21", "TK8"),
    "皇牌空战7":         ("#1f618d", "AC7"),
    "恐鬼症":            ("#5d6d7e", "PHA"),
    "灵魂摆渡人":        ("#117864", "SPR"),
    "潜水员戴夫":        ("#1a5276", "DAV"),
    "恶意不息":          ("#7b241c", "NIA"),
    "第一后裔":          ("#2e86c1", "FD"),
    "燕云十六声":        ("#8e44ad", "YY"),
    "明末渊虚之羽":       ("#7b241c", "WU"),
}

# 占位兜底用的中性色板（按名字哈希取一个，保证同一游戏颜色稳定）
NEUTRAL = ["#5b6b7c", "#6b7a8f", "#4f6d7a", "#7a6b5b", "#5f7a5f",
           "#7a5f6b", "#6b5f7a", "#7a7a5f"]


def _norm(s: str) -> str:
    """归一化名字：小写、只留字母数字。用于匹配与查表。"""
    return "".join(ch for ch in s.lower() if ch.isalnum())


def _similar(a: str, b: str) -> float:
    """0~1 的字符串相似度，用标准库 difflib，不引第三方。"""
    import difflib
    return difflib.SequenceMatcher(None, a, b).ratio()


def _name_match(query: str, candidate: str) -> bool:
    """判断 Steam 搜到的结果是不是我们要的那款。

    宁可判「不像」也不能显示一张来路不明的图 —— 错的 logo 比
    没有 logo 更误导人。判据：完全相等 / 互相包含 / 相似度够高。
    """
    q, c = _norm(query), _norm(candidate)
    if not q or not c:
        return False
    if q == c:
        return True
    # 互相包含（处理「艾尔登法环」vs「ELDEN RING」译名差异，
    # 以及「巫师3」vs「巫师3：狂猎」这种简称/全称）
    if len(q) >= 3 and len(c) >= 3 and (q in c or c in q):
        return True
    return _similar(q, c) >= 0.62


class LogoService(QObject):
    """异步取图标，取到了发 logo_ready(游戏名)。"""

    logo_ready = Signal(str)

    def __init__(self, cache_dir: str = None, parent=None):
        super().__init__(parent)
        self.cache_dir = cache_dir or os.path.join(os.path.expanduser("~"),
                                                   ".gamesavecloud", "logocache")
        os.makedirs(self.cache_dir, exist_ok=True)
        self._pixmaps = {}        # 归一化名字 -> QPixmap（内存缓存）
        self._pending = set()     # 正在请求中的名字，避免重复发请求
        self._failed = set()      # 已知取不到的名字，不再重试
        # 专门的线程池：限 4 并发，避免一次刷新把网络打满
        self._pool = QThreadPool()
        self._pool.setMaxThreadCount(4)
        # 运行中 worker 的强引用。QThreadPool.start() 不持 Python 侧引用，
        # 不自己留一份的话，GC 可能在 run() 还没跑完时把 Worker 连同它的
        # signals 一起回收，logo_ready 就永远发不出来。
        self._active = set()
        self._active_lock = threading.Lock()

    # ==========================================================
    # 对外接口
    # ==========================================================
    def pixmap(self, name: str) -> QPixmap:
        """拿当前能拿到的最好图标。永远不返回 None。"""
        key = _norm(name)
        if not key:
            return self._draw_placeholder(name or "?")
        if key in self._pixmaps:
            return self._pixmaps[key]
        # 1. 磁盘缓存
        disk = self._load_disk(key)
        if disk is not None:
            self._pixmaps[key] = disk
            return disk
        # 2. 内置预设（断网也有像样的图标）
        return self._draw_builtin(name)

    def request(self, name: str):
        """请求联网图标。已缓存/正在请求/已知失败的直接跳过。"""
        key = _norm(name)
        if not key or key in self._pending or key in self._failed:
            return
        if key in self._pixmaps:
            return
        self._pending.add(key)

        def job(_sig):
            # Worker.run() 会把 signals 作为第一个位置参数传进来，
            # 这里用不到但必须收下，否则 TypeError。
            try:
                img = self._fetch_remote(name)
            except Exception:
                img = None
            return key, name, img

        from .workers import Worker
        w = Worker(job)
        w.signals.result.connect(self._on_fetched)

        def _release(*_a):
            with self._active_lock:
                self._active.discard(w)

        w.signals.result.connect(_release)
        w.signals.error.connect(_release)
        with self._active_lock:
            self._active.add(w)
        self._pool.start(w)

    def _on_fetched(self, res):
        key, name, img = res
        self._pending.discard(key)
        # 这里已经在 GUI 线程，才允许碰 QPixmap / QPainter。
        pm = self._to_tile(img) if (img is not None and not img.isNull()) else None
        if pm is None or pm.isNull():
            self._failed.add(key)
            return
        self._pixmaps[key] = pm
        self._save_disk(key, pm)
        self.logo_ready.emit(name)
    # ==========================================================
    # 联网检索
    # ==========================================================
    def _fetch_remote(self, name: str):
        """在工作线程里只取原始图片，返回 QImage（或 None）。

        刻意不在这里转 QPixmap：QPixmap / QPainter 只能在 GUI 线程用，
        在工作线程里画图属于未定义行为（离屏环境下可能「看起来正常」，
        换个平台就崩）。所以线程只负责网络 IO 和解码，规整成图标
        留给 _on_fetched 在主线程做。
        """
        appid = self._steam_appid(name)
        if not appid:
            return None
        # logo.png 是透明底的游戏标志，最适合做小图标；取不到再用 header.jpg
        for url in (
            "https://cdn.akamai.steamstatic.com/steam/apps/%s/logo.png" % appid,
            "https://cdn.akamai.steamstatic.com/steam/apps/%s/header.jpg" % appid,
        ):
            data = self._http(url)
            if not data:
                continue
            img = QImage()
            if img.loadFromData(data, "PNG") or img.loadFromData(data, "JPG"):
                if not img.isNull():
                    return img
        return None

    def _steam_appid(self, name: str):
        """在 Steam 商店里搜这款游戏，返回 appid（不确定就返回 None）。"""
        url = ("https://store.steampowered.com/api/storesearch/"
               "?term=%s&cc=cn&l=schinese" % urllib.parse.quote(name))
        raw = self._http(url)
        if not raw:
            return None
        try:
            d = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            return None
        best = None
        for it in d.get("items", []):
            if it.get("type") and it.get("type") != "app":
                continue                      # 跳过 DLC / 原声带 / 捆绑包
            cand = it.get("name") or ""
            if _name_match(name, cand):
                # 多个候选时取相似度最高的
                if best is None or _similar(_norm(name), _norm(cand)) > best[0]:
                    best = (_similar(_norm(name), _norm(cand)), it.get("id"))
        return best[1] if best else None

    @staticmethod
    def _http(url: str, retries: int = 1):
        for i in range(retries + 1):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                # 404 = 这款游戏没有这个尺寸的图，属正常情况，不重试
                if e.code == 404:
                    return None
                if i >= retries:
                    return None
            except Exception:
                if i >= retries:
                    return None
        return None

    # ==========================================================
    # 绘制：内置预设 / 占位兜底
    # ==========================================================
    def _draw_builtin(self, name: str) -> QPixmap:
        key = _norm(name)
        spec = None
        for k, v in BUILTIN.items():
            if _norm(k) == key:
                spec = v
                break
        if spec is None:
            # 没查到的名字也按包含关系试一次（「艾尔登法环 黑夜君临」这类）
            for k, v in BUILTIN.items():
                nk = _norm(k)
                if len(nk) >= 4 and (nk in key or key in nk):
                    spec = v
                    break
        if spec is None:
            return self._draw_placeholder(name)
        color, code = spec
        return self._draw_tile(color, code, name)

    def _draw_placeholder(self, name: str) -> QPixmap:
        """首字母色块。与真实图标同尺寸同圆角，保证列表不会跳动。"""
        ch = (name or "?").strip()[:1] or "?"
        # 用 crc32 而不是 hash()：hash(str) 在 Python 里受
        # PYTHONHASHSEED 影响按进程随机化，同一款游戏每次启动
        # 颜色都会变。crc32 是确定性的。
        idx = zlib.crc32((_norm(name) or "?").encode("utf-8")) % len(NEUTRAL)
        return self._draw_tile(NEUTRAL[idx], ch.upper(), name)

    @staticmethod
    def _draw_tile(color: str, label: str, full_name: str) -> QPixmap:
        """画一个圆角色块：竖向渐变底 + 居中短代号 + 细内描边。

        内置预设和占位兜底走同一条绘制路径，所以两者在尺寸、
        圆角、内边距上完全一致 —— 图标到位后替换不会引起布局抖动。
        """
        pm = QPixmap(TILE, TILE)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        r = TILE * 0.22                      # 圆角半径
        base = QColor(color)
        dark = base.darker(128)
        grad = QLinearGradient(0, 0, 0, TILE)
        grad.setColorAt(0.0, base.lighter(112))
        grad.setColorAt(1.0, dark)
        p.setPen(Qt.NoPen)
        p.setBrush(grad)
        p.drawRoundedRect(0, 0, TILE, TILE, r, r)
        # 内描边，让色块在浅色列表里更实一点
        p.setPen(QPen(QColor(255, 255, 255, 70), 2))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(1, 1, TILE - 2, TILE - 2, r, r)

        # 代号：短代号用小字号全显示，单个首字母用大字号
        # 注意 QFont 的第二个位置参数是 pointSize(int)，不是字重 ——
        # 字重要单独 setWeight。这里反正用像素字号，直接重建。
        f = QFont("Microsoft YaHei UI")
        f.setWeight(QFont.DemiBold)
        f.setPixelSize(int(TILE * (0.30 if len(label) <= 2 else 0.24)))
        p.setFont(f)
        p.setPen(QColor(255, 255, 255, 235))
        fm = QFontMetrics(f)
        txt = label if len(label) <= 3 else label[:3]
        while fm.horizontalAdvance(txt) > TILE * 0.78 and len(txt) > 1:
            txt = txt[:-1]
        p.drawText(pm.rect(), Qt.AlignCenter, txt)
        p.end()
        pm.setDevicePixelRatio(1.0)
        return pm

    # ==========================================================
    # 位图规整：任意来源的图都变成同样尺寸的圆角方块
    # ==========================================================
    @staticmethod
    def _to_tile(img: QImage) -> QPixmap:
        if img.isNull():
            return None
        # 透明底的图（Steam logo.png）用 contain，保证标志不被裁；
        # 不透明的图（header.jpg 这类横长封面）用 cover 居中裁切填满。
        has_alpha = img.hasAlphaChannel() and img.pixel(img.width() // 2,
                                                        img.height() // 2) >> 24 != 255
        src = img.scaled(TILE, TILE,
                         Qt.KeepAspectRatio if has_alpha else Qt.KeepAspectRatioByExpanding,
                         Qt.SmoothTransformation)
        if has_alpha:
            src = src.copy((src.width() - TILE) // 2, (src.height() - TILE) // 2,
                           TILE, TILE)
        pm = QPixmap(TILE, TILE)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(0, 0, TILE, TILE, TILE * 0.22, TILE * 0.22)
        p.setClipPath(path)
        p.drawImage(0, 0, src)
        p.end()
        return pm

    # ==========================================================
    # 磁盘缓存
    # ==========================================================
    def _disk_path(self, key: str) -> str:
        h = hashlib.md5(key.encode("utf-8")).hexdigest()
        return os.path.join(self.cache_dir, h + ".png")

    def _load_disk(self, key: str):
        p = self._disk_path(key)
        if not os.path.isfile(p):
            return None
        pm = QPixmap(p)
        return pm if not pm.isNull() else None

    def _save_disk(self, key: str, pm: QPixmap):
        try:
            pm.save(self._disk_path(key), "PNG")
        except Exception:
            pass
