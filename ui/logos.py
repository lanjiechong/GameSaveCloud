# -*- coding: utf-8 -*-
"""
游戏图标（logo）加载服务。

逐级保障，任何一层失败都不会出现空白、破图或布局错位：

    1. 匹配落盘      按 game_id 存下的图标（匹配阶段写入），换名也不丢
    2. 磁盘缓存      按名字哈希的旧版缓存，兼容历史数据
    3. 可选 logo 包  用户自行安装到本机的离线 logo 包
                     （%LOCALAPPDATA%\\GameSaveCloud\\logopack\\，
                     或开发目录 ui/assets/logos/，两者都可缺省）。
                     **仓库与发布包不含任何第三方游戏 logo** —— 商标素材
                     只在运行时从 Steam CDN 获取，不在本项目里再分发。
    4. 内置预设      预置配色与代号，**断网也立刻可画**
    5. 占位兜底      全部失败时画一个「首字母色块」，
                     尺寸、圆角、位置与真实图标完全一致

本模块**只做本地、全程同步返回、绝不联网**。真正的联网补齐（搜 Steam、
下 logo、解析中文名）由 metaservice.MetaService 在后台线程池里做，
成功后落盘再回填到这里 —— 列表永远是先出来、图后跟上，
不会因为网络卡住整个界面。

图标来源（由 metaservice 驱动）：
    Steam 商店搜索 API（免密钥）
        https://store.steampowered.com/api/storesearch/?term=<游戏名>&cc=cn&l=schinese
     返回 appid 后取图片：
        https://cdn.akamai.steamstatic.com/steam/apps/<appid>/logo.png     ← 透明底，首选
        https://cdn.akamai.steamstatic.com/steam/apps/<appid>/header.jpg   ← 兜底

为什么用 Steam：游戏覆盖面最广、免申请 key、有公开 CDN。
风险是名称匹配可能对不上，所以加了一道相似度校验（gamesave.gamemeta）——
对不上就当作没找到，回落到预设/占位，绝不显示一张来路不明的图。

关于名字：扫描器给游戏起的是「中文俗名 英文名」组合名
（如「荒野大镖客2 Red Dead Redemption 2」），而 Steam 只认官方名，
整个组合名丢进去搜不到任何东西。所以检索会拆出英文部分优先搜；
可选离线 logo 包则用「精确 / 包含 / 分词」四级名字匹配，天然兼容组合名。

可选离线 logo 包的构建：scripts/fetch_top_logos.py（维护工具，需联网+Pillow，
产物默认写到开发目录 ui/assets/logos/，已被 .gitignore 排除；
也可加 --out 输出到 %LOCALAPPDATA%\\GameSaveCloud\\logopack\\）。

QPixmap / QPainter 只能在 GUI 线程用，所以本模块的所有公开方法都
必须在主线程调用；工作线程只允许用 fetch_bytes / fetch_logo_image
（它们只跟 bytes 和 QImage 打交道）。
"""
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
import zlib

from PySide6.QtCore import Qt
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QImage, QLinearGradient,
                           QPainter, QPainterPath, QPen, QPixmap)

from gamesave import gamemeta
# 名字归一化只有一份实现（在 gamesave/gamemeta.py），这里用别名保留
# 原有调用写法，避免两处逻辑各自漂移。
from gamesave.gamemeta import has_cjk, norm_name as _norm

TILE = 96                      # 图标绘制尺寸（超采样，显示时缩到 40 左右更清晰）
UA = "Mozilla/5.0 (compatible; GameSaveCloud/1.2)"
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


# ---------------------------------------------------------------
# 可选离线 logo 包（scripts/fetch_top_logos.py 生成，不随仓库/发布包分发）
# ---------------------------------------------------------------
def _user_pack_dir() -> str:
    """用户自行安装的离线 logo 包目录（跨开发版/打包版都生效）。"""
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = os.path.expanduser("~")
    return os.path.join(base, "GameSaveCloud", "logopack")


def _pack_dirs() -> list:
    """logo 包候选目录，按优先级排列；不存在的目录由调用方跳过。

    1. 用户目录 %LOCALAPPDATA%/GameSaveCloud/logopack（用户自己装的包）
    2. 打包目录 _MEIPASS/ui/assets/logos（私有构建可选烘焙）
    3. 开发目录 ui/assets/logos（fetch_top_logos.py 默认产物，gitignore）
    """
    dirs = [_user_pack_dir()]
    if getattr(sys, "_MEIPASS", None):
        dirs.append(os.path.join(sys._MEIPASS, "ui", "assets", "logos"))
    dirs.append(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "assets", "logos"))
    return dirs


_PACK = None          # {"<appid>": ["名1", "名2", ...]}，懒加载
_PACK_INDEX = None    # 归一化名 -> appid，精确命中用


def _load_pack():
    """读所有候选目录里的 logo_pack.json 并合并。

    用户目录的包优先（同名 appid 以先加载的为准）；任何一个包缺失
    或损坏都静默跳过，零配置时返回空包，后续走联网检索。
    """
    global _PACK, _PACK_INDEX
    if _PACK is not None:
        return _PACK
    _PACK, _PACK_INDEX = {}, {}
    for d in _pack_dirs():
        try:
            with open(os.path.join(d, "logo_pack.json"),
                      encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        for appid, ent in (data.get("games") or {}).items():
            names = [n for n in (ent.get("names") or []) if n]
            if not names:
                continue
            _PACK.setdefault(appid, [])
            for n in names:
                if n not in _PACK[appid]:
                    _PACK[appid].append(n)
                k = _norm(n)
                if k and k not in _PACK_INDEX:
                    _PACK_INDEX[k] = appid
    return _PACK


def _pack_lookup(name: str):
    """在已安装的离线 logo 包里按名字找 appid，找不到返回 None。

    游戏名可能是「中文俗名 英文名」的组合（扫描器就这么起名的），
    所以按四轮找，从严到宽：
        1. 整个名字精确命中索引
        2. 整个名字包含某个包内名 —— 「荒野大镖客2 Red Dead Redemption 2」
           归一化后包含 "reddeadredemption2"，直接命中；
           多个命中时取最长的，避免「巫师3」抢在「巫师3狂猎」前面
        3. 按空格切开逐个 token 精确命中 —— 短中文名如「巫师3」
           归一化后只有 3 字符，够不上第 2 轮的长度下限，
           但作为独立 token 能精确命中
        4. token 被某个包内名包含（仅这一方向，反向会出现
           「Dead」命中 deadcells 这类误判）
    """
    _load_pack()
    if not _PACK:
        return None
    key = _norm(name)
    if not key:
        return None
    if key in _PACK_INDEX:
        return _PACK_INDEX[key]
    best = None
    for appid, names in _PACK.items():
        for n in names:
            nk = _norm(n)
            if len(nk) >= 4 and nk in key:
                if best is None or len(nk) > best[0]:
                    best = (len(nk), appid)
    if best:
        return best[1]
    for tok in (name or "").split():
        if _norm(tok) in _PACK_INDEX:
            return _PACK_INDEX[_norm(tok)]
    for tok in (name or "").split():
        tk = _norm(tok)
        if len(tk) < 4:
            continue
        for appid, names in _PACK.items():
            for n in names:
                nk = _norm(n)
                if len(nk) >= 4 and nk in tk:
                    return appid
    return None


class LogoService:
    """按游戏取图标。

    只做「本地能拿到什么」：匹配阶段落盘的图标 → 按名字的磁盘缓存 →
    可选离线包 → 内置绘制预设 → 占位兜底，全部同步返回，不联网。
    联网补齐由 metaservice.MetaService 负责，两者职责分开。
    """

    def __init__(self, cache_dir: str = None, icons_dir: str = None):
        self.cache_dir = cache_dir or os.path.join(os.path.expanduser("~"),
                                                   ".gamesavecloud", "logocache")
        # 按 game_id 存的图标：匹配成功后落盘，路径记进 game_meta.logo_file。
        # 与按名字哈希的 logocache 分开：改名、换语言、重装都不会丢图。
        self.icons_dir = icons_dir or os.path.join(
            os.path.dirname(self.cache_dir), "gameicons")
        os.makedirs(self.cache_dir, exist_ok=True)
        os.makedirs(self.icons_dir, exist_ok=True)
        self._pixmaps = {}        # 归一化名字 / "g:<game_id>" -> QPixmap（内存缓存）

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
        # 2. 可选 logo 包（用户自行安装，真实 logo 离线可用；没装就跳过）
        packed = self._load_pack_pixmap(name)
        if packed is not None:
            self._pixmaps[key] = packed
            return packed
        # 3. 内置绘制预设（断网也有像样的图标）
        return self._draw_builtin(name)

    # ---------- 按 Game 记录取图（匹配阶段落盘的那张优先） ----------
    def pixmap_for_game(self, game) -> QPixmap:
        """取某款游戏的图标。永远不返回 None。

        优先级：按 game_id 落盘的图标 → 按名字的既有链路
        （磁盘缓存 → 离线包 → 内置预设 → 占位）。
        前者是「匹配成功后持久化」的成果，换个名字也不会丢；
        后者保证任何一个没匹配过的游戏也有图可画。
        """
        gid = self._safe_id(getattr(game, "id", ""))
        if gid:
            key = "g:" + gid
            if key in self._pixmaps:
                return self._pixmaps[key]
            p = self._icon_path(getattr(game, "logo_file", ""))
            if p:
                pm = QPixmap(p)
                if not pm.isNull():
                    self._pixmaps[key] = pm
                    return pm
        return self.pixmap(getattr(game, "name", "") or "")

    def has_game_icon(self, game) -> bool:
        """这个游戏算不算「已经有图标」。

        两条来源都算：匹配阶段落盘的，或用户装的离线包里本来就有。
        后者不占额外空间，也没必要再联网下一份。
        """
        if self._icon_path(getattr(game, "logo_file", "")):
            return True
        return self.has_pack_logo(getattr(game, "name", "") or "")

    def has_pack_logo(self, name: str) -> bool:
        """名字能在离线包里命中、且图片文件确实存在。

        只命中索引但文件缺失（包不完整）不算有图 —— 否则这款游戏
        永远没机会联网取图。
        """
        appid = _pack_lookup(name)
        if not appid:
            return False
        return any(os.path.isfile(os.path.join(d, "%s.png" % appid))
                   for d in _pack_dirs())

    def pack_name_zh(self, appid) -> str:
        """按 appid 从离线包里反查中文名。

        Steam 国区对不少游戏只返回英文名（官方没做中文本地化），
        但社区通用译名在离线包的 names 里通常有一条。用它补，
        比让这款游戏永远「缺中文名」要好。
        """
        _load_pack()
        for n in _PACK.get(str(appid or ""), []):
            n = (n or "").strip()
            if n and has_cjk(n):
                return n
        return ""

    def save_game_icon(self, game_id: str, pm: QPixmap) -> str:
        """把规整好的图标写成 <game_id>.png，返回文件名（存库用）。

        先写 .tmp 再原子替换：中途被 kill 也不会留下半张图，
        否则下次启动读到损坏 PNG，QPixmap 为 null，图标变空白。
        """
        if pm is None or pm.isNull():
            return ""
        base = self._safe_id(game_id)
        if not base:
            return ""
        name = base + ".png"
        dst = os.path.join(self.icons_dir, name)
        tmp = dst + ".tmp"
        try:
            os.makedirs(self.icons_dir, exist_ok=True)
            if not pm.save(tmp, "PNG"):
                return ""
            os.replace(tmp, dst)
        except Exception:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            return ""
        self._pixmaps["g:" + base] = pm
        return name

    def _icon_path(self, file_name: str):
        """文件名 → 绝对路径。basename 兜一道，防止库里存了 ../ 之类。"""
        base = os.path.basename(file_name or "")
        if not base:
            return None
        p = os.path.join(self.icons_dir, base)
        return p if os.path.isfile(p) else None

    @staticmethod
    def _safe_id(game_id: str) -> str:
        """game_id 只留字母数字和 - _，防止拼路径时跑出图标目录。"""
        return "".join(ch for ch in (game_id or "")
                       if ch.isalnum() or ch in "-_")

    def fetch_bytes(self, url: str):
        """带一次重试的 GET，返回 bytes 或 None。工作线程可调。"""
        return self._http(url)

    def fetch_logo_image(self, appid):
        """下图标原始字节并解码成 QImage。可在工作线程调用（QImage 是安全的）。

        刻意不返回 QPixmap：QPixmap 只能在 GUI 线程创建。线程里只做
        网络 IO 和解码，规整成 96px 圆角方块留给主线程。
        """
        for url in gamemeta.logo_urls(appid):
            data = self._http(url)
            if not data:
                continue
            img = QImage()
            if img.loadFromData(data, "PNG") or img.loadFromData(data, "JPG"):
                if not img.isNull():
                    return img
        return None

    def _load_pack_pixmap(self, name: str):
        """按名字从可选 logo 包里取图；没装包或包里没有都返回 None。"""
        appid = _pack_lookup(name)
        if not appid:
            return None
        for d in _pack_dirs():
            p = os.path.join(d, "%s.png" % appid)
            if not os.path.isfile(p):
                continue
            pm = QPixmap(p)
            if not pm.isNull():
                return pm
        return None

    # ==========================================================
    # 网络：取原始字节 / 解码成 QImage（均可在工作线程调用）
    # ==========================================================
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
    def to_tile(img: QImage) -> QPixmap:
        """把任意来源的图规整成 96px 圆角方块。只能在 GUI 线程调用。"""
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
