# -*- coding: utf-8 -*-
r"""
游戏元数据解析：中文名 + Steam appid + 图标来源。

为什么单独一个模块：
    这三件事都是「网络 IO + 字符串匹配」，与界面无关，也不该依赖 PySide6。
    放在 gamesave/ 下，tests/test_core.py 用一个假的 fetch 函数就能把
    「搜到了 / 没搜到 / 网络炸了 / 名字不像」四条分支全覆盖，不需要真联网。

分工：
    本模块只负责「算出该用什么名字、该去哪个 URL 取图」，不碰文件系统，
    也不碰 QPixmap（QPixmap 只能在 GUI 线程用）。图标文件落盘、数据库
    写入由 ui/metaservice.py 在主线程完成。

匹配结果落在 vault 的 game_meta 表（见 Vault.save_meta）。

为什么搜 Steam 而不是别处：
    覆盖面最广、免密钥、有公开 CDN，而且 storesearch 的 `l=schinese`
    直接返回本地化后的商店名 —— 这就是我们要的「中文名」。
"""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (compatible; GameSaveCloud/1.2)"
TIMEOUT = 12

SEARCH_URL = ("https://store.steampowered.com/api/storesearch/"
              "?term=%s&cc=cn&l=schinese")
# 跨语言兜底时才用：拿官方英文名回来做比对
DETAILS_URL = ("https://store.steampowered.com/api/appdetails"
               "?appids=%s&l=english&filters=basic")
# logo.png 是透明底的游戏标志，最适合做小图标；取不到再用 header.jpg
LOGO_URLS = ("https://cdn.akamai.steamstatic.com/steam/apps/%s/logo.png",
             "https://cdn.akamai.steamstatic.com/steam/apps/%s/header.jpg")

# 匹配状态（落库用字符串，便于人工排查）
STATUS_OK = "ok"                 # Steam 查到了这款游戏
STATUS_NOT_FOUND = "not_found"   # 搜遍了也没搜到
STATUS_ERROR = "error"           # 网络/解析失败

# 失败退避：第 1 次 10 分钟，之后每次翻倍，封顶 7 天。
# 没有退避的话，一款 Steam 上根本没有的游戏每次刷新都要打一轮请求，
# 而且注定失败 —— 既慢又容易被 Steam 限流。
RETRY_BASE = 600.0
RETRY_MAX = 7 * 86400.0

# 只认汉字（含扩展 A 与兼容区）。刻意不含平假名/片假名/谚文：
# 日文原名或韩文名不是中文名，混进来会让「中文名」这个字段名不副实。
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


# ==================================================================
# 字符串工具（logos.py 也用这几个，保持唯一实现）
# ==================================================================
def has_cjk(text: str) -> bool:
    """名字里是否含汉字。"""
    return bool(_CJK.search(text or ""))


def norm_name(text: str) -> str:
    """归一化：小写、只留字母数字。用于匹配与查表。"""
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


def similar(a: str, b: str) -> float:
    """0~1 的字符串相似度，用标准库 difflib。"""
    import difflib
    return difflib.SequenceMatcher(None, a, b).ratio()


# 副标题分隔符：英文名靠这些符号接一截是正常的产品命名
# （Sekiro → Sekiro™: Shadows Die Twice - GOTY Edition）
_SUBTITLE_SEP = set(":：-—–([™®|/")


def _is_subtitle_ext(name: str, candidate: str) -> bool:
    """candidate 是不是「name + 副标题分隔符 + 副标题」这种扩展。"""
    n = (name or "").strip().lower()
    c = (candidate or "").strip().lower()
    if not n or not c.startswith(n):
        return False
    rest = c[len(n):].lstrip()
    return bool(rest) and rest[0] in _SUBTITLE_SEP


def name_match(query: str, candidate: str) -> bool:
    """判断 Steam 搜到的结果是不是我们要的那款。

    宁可判「不像」也不能显示一张来路不明的图 —— 错的 logo 比没有更误导。
    判据：完全相等 / 互相包含 / 相似度够高。
    """
    q, c = norm_name(query), norm_name(candidate)
    if not q or not c:
        return False
    if q == c:
        return True
    # 查询词完整出现在候选名里：可信度最高的一种包含关系。
    # 两个汉字的短名（「只狼」vs「只狼：影逝二度」）也算 —— 一个两字
    # 中文词的信息量远大于两个拉丁字母，不能一律卡 len>=3，
    # 否则所有两字标题的游戏永远匹配不上。
    if q in c:
        if len(q) >= 3 or (has_cjk(q) and len(q) >= 2):
            # 英文名后面被追加一截，多半是 DLC / MOD 工具
            # （Cyberpunk 2077 → Cyberpunk 2077 REDmod），别认成同一款；
            # 中文名加副标题则是正常译名（只狼 → 只狼：影逝二度），
            # 中文本身就短，用长度比去卡会大面积误伤，所以两种语言区别对待。
            if has_cjk(q) or len(c) <= len(q) * 1.3:
                return True
            # 靠分隔符接副标题是正常命名（Sekiro™: Shadows Die Twice），
            # 靠空格硬接一截则多半是另一个产品（Cyberpunk 2077 REDmod）
            if _is_subtitle_ext(query, candidate):
                return True
        # 包含关系已经不成立了，不能再靠相似度蒙混过关 ——
        # 0.774 的相似度正好能把 "Cyberpunk 2077 REDmod" 放进来。
        return False
    # 反向包含（候选是查询的一部分）容易误判（"dead" 会命中 deadcells），
    # 所以只允许够长的候选走这条路
    if c in q and len(c) >= 4:
        return True
    # 相似度兜底。必须加首字符与长度约束 —— 否则共享一个词就会被认成同一款：
    #   "Supermarket Together" vs "Don't Starve Together"（共享 Together）
    #   "Ogu Forest"           vs "The Forest"（共享 Forest）
    # 实测这两对都能混过 0.62 的阈值，把 A 游戏的图标安到 B 头上。
    if q[0] != c[0]:
        return False
    short, long_ = sorted((len(q), len(c)))
    if short / long_ < 0.6:
        return False
    return similar(q, c) >= 0.75


# 常见缩写：Steam 搜索只认全名，而扫描器的游戏名/目录名常用缩写。
# 只收无歧义的几个，别把这里做成第二张游戏表。
ALIASES = {
    "gtav": "Grand Theft Auto V",
    "gta5": "Grand Theft Auto V",
    "gtaiv": "Grand Theft Auto IV",
    "rdr2": "Red Dead Redemption 2",
    "mhw": "Monster Hunter: World",
    "ds3": "DARK SOULS III",
}


def _camel_split(text: str) -> str:
    """连写名拆词：SplitFiction -> Split Fiction。

    目录名经常是去空格的驼峰写法，原样丢进 Steam 搜不到任何东西。
    全大写（AIHASTO / DDTNL）拆不开，返回空串表示"没变体"。
    """
    t = (text or "").strip()
    if not t or " " in t or has_cjk(t):
        return ""
    split = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", t)
    return split if split != t else ""


# 目录名常带这些冗余尾词，Steam 上的正式名没有（MiSideFull -> MiSide）
_SUFFIXES = ("full", "game", "plus", "remastered", "definitive", "complete")


def _strip_suffix(text: str) -> str:
    """去掉整词尾的冗余后缀；动不到词中间，也要求剩下够长。"""
    t = (text or "").strip()
    low = t.lower()
    for suf in _SUFFIXES:
        if low.endswith(suf) and len(t) > len(suf) + 2:
            return t[:-len(suf)].strip()
    return ""


def search_terms(name: str) -> list:
    """从游戏名生成搜索候选词，越可能命中的越靠前。

    扫描器起的名字是「中文俗名 英文名」组合名
    （如「荒野大镖客2 Red Dead Redemption 2」）。Steam 的搜索只认官方名，
    把整个组合名丢进去一个结果都没有（实测：term=荒野大镖客2 Red Dead
    Redemption 2 → 空；单独搜 Red Dead Redemption 2 → 1174180）。

    顺序：变形词（拆驼峰 / 去后缀）→ 别名全名 → 英文部分 → 全名 → 中文部分。
    变形词排最前，因为目录名连写是最常见的形态（SplitFiction、
    TheFarmerWasReplaced），原样搜必然落空。
    """
    words = (name or "").split()
    ascii_run = " ".join(w for w in words if w.isascii() and w.strip())
    zh_run = "".join(w for w in words if not w.isascii() and w.strip())

    base = []
    alias = ALIASES.get(norm_name(name))
    if alias:
        base.append(alias)
    for t in (ascii_run, name, zh_run):
        t = (t or "").strip()
        if t and t not in base:
            base.append(t)

    variants = []
    for t in base:
        # 去后缀优先于拆驼峰：MiSideFull 想要的是 MiSide，
        # 而不是把它拆成 "Mi Side Full"（那个也留着，只是排在后面）
        for v in (_strip_suffix(t), _camel_split(t)):
            if v and v not in variants and v not in base:
                variants.append(v)

    out = []
    for t in variants + base:
        if t and t not in out:
            out.append(t)
    return out


def pick_chinese(names) -> str:
    """从一组候选名里挑第一个含汉字的，没有就返回空串。"""
    for n in names or ():
        n = (n or "").strip()
        if n and has_cjk(n):
            return n
    return ""


def logo_urls(appid) -> tuple:
    """按优先级给出这款游戏图标的 CDN 地址。"""
    appid = str(appid or "").strip()
    if not appid:
        return ()
    return tuple(u % appid for u in LOGO_URLS)


# ==================================================================
# 网络
# ==================================================================
def default_fetch(url: str, retries: int = 1) -> bytes:
    """默认取数函数。失败重试一次再抛异常，由 lookup() 统一归类。

    Steam 在国内偶发丢连接（实测同一接口 1s / 8s / 超时轮着来），
    不重试的话一次抖动就会把整款游戏判成 error，用户看到的就是
    「失败 N 款」—— 这是最容易被误认为「功能没做好」的假失败。
    """
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return b""          # 明确没有这个资源，不算错误
            last = e
        except Exception as e:
            last = e
        if i < retries:
            time.sleep(0.8)
    raise last if last else OSError("请求失败")


def _parse_items(raw):
    """解析 storesearch 的响应；不是合法 JSON 返回 None。"""
    try:
        d = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return None
    items = d.get("items")
    return items if isinstance(items, list) else None


def _best_match(term: str, items):
    """在搜索结果里挑相似度最高、且名字校验通过的那条。"""
    best = None
    for it in items:
        if it.get("type") and it.get("type") != "app":
            continue                      # 跳过 DLC / 原声带 / 捆绑包
        cand = (it.get("name") or "").strip()
        if not name_match(term, cand):
            continue
        score = similar(norm_name(term), norm_name(cand))
        if best is None or score > best[0]:
            best = (score, it)
    return best[1] if best else None


def _ok(appid, official: str, zh_candidates) -> dict:
    """拼一个 status=ok 的结果。"""
    return {"status": STATUS_OK,
            "appid": str(appid or ""),
            "official_name": (official or "").strip(),
            "name_zh": pick_chinese(zh_candidates or ()),
            "error": "",
            "source": "steam"}


# ==================================================================
# 补充源：Bangumi（只补中文名）
# ==================================================================
# 为什么不接其它几家（2026-10-30 本机实测）：
#   api.bgm.tv            连接超时
#   www.wikidata.org      连接超时，且要「搜 QID → 再取 zh label」两步
#   en.wikipedia.org      连接超时
#   api.rawg.io           连接超时，且需要 API key（免费额度 2 万次/月）
#   steamspy.com          403（Cloudflare 挑战页，拿不到 JSON）
# 能连上的只有 Steam 官方接口。Bangumi 结构最简单（一次请求、免密钥、
# 直接返回 name_cn），所以作为唯一的中文名补充源接入，**默认关闭** ——
# 打不通的源常开会给每次刷新白加一个超时。
BANGUMI_SEARCH = ("https://api.bgm.tv/search/subject/%s"
                  "?type=4&responseGroup=small")


def bangumi_name_zh(name: str, fetch=None) -> str:
    """从 Bangumi 取官方中文名，取不到返回空串。

    Bangumi 是中文 ACG/游戏条目库（type=4 是游戏），日系与独立游戏覆盖
    最好，单机 3A 也有条目但不如 Steam 全。它的 ``name_cn`` 比 Steam 的
    ``l=schinese`` 更愿意给中文 —— Steam 国区对 RDR2 这类只返回英文名。

    只接受名字校验通过的条目：宁可不补，也不能把《巫师3》的中文名
    安到《巫师之昆特牌》头上。
    """
    fetch = fetch or default_fetch
    q = (name or "").strip()
    if not q:
        return ""
    try:
        raw = fetch(BANGUMI_SEARCH % urllib.parse.quote(q))
    except Exception:
        return ""
    if not raw:
        return ""
    try:
        d = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return ""
    for it in (d.get("list") or [])[:5]:
        zh = (it.get("name_cn") or "").strip()
        orig = (it.get("name") or "").strip()
        if not zh:
            continue
        if name_match(q, orig) or name_match(q, zh):
            return zh
    return ""


def _english_name(appid, fetch) -> str:
    """问 appdetails 要这款游戏的官方英文名；失败返回空串。"""
    try:
        raw = fetch(DETAILS_URL % appid)
    except Exception:
        return ""
    if not raw:
        return ""
    try:
        d = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return ""
    ent = d.get(str(appid)) or {}
    return ((ent.get("data") or {}).get("name") or "").strip()


def _match_prefer_first(term: str, items, fetch):
    """先看搜索结果的第一条 —— 它八成就是本体。

    分两种情况：
    - 名字能直接对上（同语言）→ 直接用，不额外发请求
    - 名字是中文而查询是英文（国区常态）→ 问 appdetails 要英文名再判

    为什么必须排在宽松匹配之前：国区 storesearch 直接返回本地化名
    （「赛博朋克 2077」），拿英文查询词算字符串相似度必然是 0，
    宽松匹配就会挑中名字里恰好带查询词的 DLC / 工具
    （实测：Cyberpunk 2077 命中 2060310 REDmod，而不是本体 1091500；
    搜 SplitFiction 明明返回了「双影奇境」却一条都匹配不上）。

    返回带 `_local` 字段（搜索结果里的原名）的候选 dict，或 None。
    """
    first = next((it for it in items
                  if (it.get("type") or "app") == "app" and it.get("id")),
                 None)
    if not first:
        return None
    local = (first.get("name") or "").strip()
    if name_match(term, local):
        return dict(first, _local=local)
    # 只有「查询是英文、候选是中文」才值得去问英文名 —— 跨语言是唯一
    # 靠字符串比不出来的场景。两边同为英文却对不上，基本就是另一款
    # 游戏，多打一次 appdetails 只会拖慢整批匹配（该接口还有频率限制）。
    if not (term.isascii() and has_cjk(local)):
        return None
    english = _english_name(str(first.get("id")), fetch)
    if not english:
        return None
    q, c = norm_name(term), norm_name(english)
    # 跨语言兜底本身就是在「猜」，阈值要比常规校验严：
    # 0.8 能收下 "SplitFiction" vs "Split Fiction"（归一化后同形）这类
    # 空格差异，同时把 "... Phantom Liberty" 这种资料片挡在外面。
    if q == c or similar(q, c) >= 0.8:
        return dict(first, name=english, _local=local)
    return None


def lookup(name: str, fetch=None, local_index=None,
           use_bangumi: bool = False, hints=None) -> dict:
    """查一款游戏在 Steam 上的身份。

    三层，从快到慢：
        1. local_index（可选）—— 本地离线索引，命中即返回，零网络
        2. Steam storesearch + appdetails —— 主源，覆盖面最广
        3. Bangumi（可选，默认关）—— 只补中文名

    hints：额外的候选名，通常是「从存档路径推断出来的游戏名」。
        LocalLow 的结构是 LocalLow\\<工作室>\\<游戏>，扫描器早期只取到
        工作室名（AIHASTO / House House），拿这个去 Steam 搜当然搜不到。
        调用方把路径里的真实目录名传进来，这里按顺序试，命中即止。

    返回 dict：
        status        ok / not_found / error
        appid         Steam appid（字符串，查不到为空）
        official_name 商店里的英文名
        name_zh       中文名，没有则为空串
        error         仅 status=error 时有意义
        source        index / steam / steam+bangumi

    注意 status=ok 但 name_zh 为空是**正常结果**：大量独立游戏在 Steam
    国区只有英文名。调用方要把「查到了但没中文名」与「没查到」区分开，
    否则这类游戏会被无限重试。
    """
    fetch = fetch or default_fetch
    res = {"status": STATUS_NOT_FOUND, "appid": "", "name_zh": "",
           "official_name": "", "error": "", "source": ""}

    # 候选名：路径线索优先（更可能是真游戏名），原名垫后
    cands = []
    for h in list(hints or ()) + [name]:
        h = (h or "").strip()
        if h and h not in cands:
            cands.append(h)
    if not cands:
        return res

    # 1. 本地索引优先。不产生网络请求，断网时也能认出榜单上的游戏。
    if local_index is not None:
        for c in cands:
            try:
                hit = local_index(c)
            except Exception:
                hit = None
            if hit:
                return hit

    # 2. Steam：所有候选名展开成搜索词，按「变形词 → 原名」的顺序试
    terms = []
    for c in cands:
        for t in search_terms(c):
            if t and t not in terms:
                terms.append(t)

    any_parsed = False
    last_error = ""
    for term in terms:
        try:
            raw = fetch(SEARCH_URL % urllib.parse.quote(term))
        except Exception as e:
            last_error = "%s: %s" % (type(e).__name__, e)
            continue
        if not raw:
            last_error = "空响应"
            continue
        items = _parse_items(raw)
        if items is None:
            last_error = "响应不是合法 JSON"
            continue
        any_parsed = True
        first = _match_prefer_first(term, items, fetch)
        if first:
            official = (first.get("name") or "").strip()
            return _with_bangumi(_ok(first.get("id"), official,
                                     [first.get("_local"), official]),
                                 term, fetch, use_bangumi)
        hit = _best_match(term, items)
        if hit:
            official = (hit.get("name") or "").strip()
            return _with_bangumi(_ok(hit.get("id"), official, [official]),
                                 term, fetch, use_bangumi)

    # 所有候选词都解析失败才算网络错误；只要有一轮成功解析，
    # 那就是「确实没搜到」，属于 not_found，不该触发重试退避的 error 分支。
    if not any_parsed and last_error:
        res["status"] = STATUS_ERROR
        res["error"] = last_error
    return res


def _with_bangumi(res: dict, name: str, fetch, enabled: bool) -> dict:
    """Steam 查到游戏但没给中文名时，可选地从 Bangumi 补一个。

    只在 status=ok 时补：没查到 appid 的话，光有中文名也没法取图标，
    徒增一次必然超时的请求。
    """
    if not enabled or res.get("status") != STATUS_OK or res.get("name_zh"):
        return res
    zh = bangumi_name_zh(name, fetch)
    if zh:
        res["name_zh"] = zh
        res["source"] = "steam+bangumi"
    return res


# ==================================================================
# 从存档路径推断游戏名
# ==================================================================
# 路径里这些段不是游戏名，别拿去搜 Steam。
# 后半段是 Chromium / Electron / Unity 的缓存目录 —— 有些软件会把它们
# 直接铺在 Roaming 根下，拿去搜 Steam 会撞上同名的小游戏（实测
# "Crashpad" 命中了 Steam 上的 "Crashpad: Cozy Idle Outpost"）。
GENERIC_DIRS = {
    "save", "saves", "saved", "savegames", "savegame", "savedata",
    "savefiles", "data", "userdata", "profile", "profiles", "settings",
    "config", "configs", "cache", "caches", "logs", "log", "storage",
    "remote", "local", "locallow", "roaming", "temp", "tmp", "documents",
    "saved games", "my games", "steam", "steamapps", "common", "backup",
    "backups", "slot", "slots", "bin", "content", "appdata", "users",
    "code cache", "gpucache", "shadercache", "crashpad", "crash reports",
    "dictionaries", "blob_storage", "local storage", "session storage",
    "indexeddb", "service worker", "unity", "il2cpp", "mono", "resources",
    "streamingassets", "webcache", "gpu cache", "dawncache", "grshadercache",
    "launcher", "titles", "social club",
}

_USERNAME = (os.environ.get("USERNAME") or "").strip().lower()


def path_hints(paths, limit: int = 4) -> list:
    """从存档路径推断可能的游戏名。

    LocalLow 的结构是 ``LocalLow\\<工作室>\\<游戏>``。扫描器早期版本只取到
    工作室那一层（AIHASTO / House House / Team Salvato），拿它去 Steam 搜
    必然落空 —— 这些名字在 Steam 上对应的是发行商，不是任何一款游戏。

    真游戏名在下一层目录里，这里把它捞出来当搜索词：
        LocalLow\\AIHASTO       -> MiSideFull
        LocalLow\\House House   -> Big Walk
        LocalLow\\Team Salvato  -> Doki Doki Literature Club Plus

    子目录只在 LocalLow 下取。Roaming / Local 的子目录是软件自己的缓存
    （Code Cache、Crashpad、GameProxyCrashReports…），拿它们去搜只会撞上
    同名小游戏 —— 实测 ACLOS（一个加速器）就这样被认成 R.E.P.O.。

    优先级：LocalLow 子目录 > 路径末段从后往前。通用目录与用户名跳过。
    """
    hints = []

    def push(n):
        n = (n or "").strip()
        if not n or len(n) < 3 or n.isdigit() or n in hints:
            return
        low = n.lower()
        if low in GENERIC_DIRS or (_USERNAME and low == _USERNAME):
            return
        hints.append(n)

    for p in paths or ():
        if "locallow" in (p or "").replace("\\", "/").lower():
            try:
                subs = sorted(d for d in os.listdir(p)
                              if os.path.isdir(os.path.join(p, d)))
            except OSError:
                subs = []
            for d in subs:
                push(d)
        parts = [x for x in os.path.normpath(p).split(os.sep) if x]
        for name in reversed(parts[-6:]):
            push(name)
    return hints[:limit]


# ==================================================================
# 调度判定
# ==================================================================
def retry_delay(attempts) -> float:
    """失败 attempts 次后，隔多久再试。指数退避，封顶 7 天。"""
    n = max(int(attempts or 0), 1)
    return min(RETRY_BASE * (2 ** (n - 1)), RETRY_MAX)


def has_chinese_name(game_name: str, meta=None) -> bool:
    """这个游戏算不算「已经有中文名」。

    三种算有：
      1. 匹配到过中文名
      2. 原名本身就含汉字（扫描器查表出来的「荒野大镖客2 Red Dead ...」）
      3. 查过 Steam 但官方确实没中文名（state=ok）
    """
    meta = meta or {}
    if (meta.get("name_zh") or "").strip():
        return True
    if has_cjk(game_name or ""):
        return True
    return meta.get("state") == STATUS_OK


def is_complete(game_name: str, meta=None, has_logo: bool = False) -> bool:
    """图标和中文名都齐了，不需要再匹配。"""
    return bool(has_logo) and has_chinese_name(game_name, meta)


def should_attempt(game_name: str, meta=None, has_logo: bool = False,
                   now=None) -> bool:
    """这次刷新要不要为它跑一轮匹配。

    已经齐了的直接跳过（这是「已有图标和中文名不重复匹配」的落点）；
    缺东西的按失败次数退避 —— 失败过一次就先等 10 分钟，
    之后每次翻倍，避免每次刷新都往 Steam 打一遍注定失败的请求。
    """
    if is_complete(game_name, meta, has_logo):
        return False
    meta = meta or {}
    if not meta:
        return True
    now = time.time() if now is None else now
    return now >= float(meta.get("next_retry_at") or 0.0)
