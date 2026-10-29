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
    return similar(q, c) >= 0.62


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


def search_terms(name: str) -> list:
    """从游戏名生成搜索候选词，越可能命中的越靠前。

    扫描器起的名字是「中文俗名 英文名」组合名
    （如「荒野大镖客2 Red Dead Redemption 2」）。Steam 的搜索只认官方名，
    把整个组合名丢进去一个结果都没有（实测：term=荒野大镖客2 Red Dead
    Redemption 2 → 空；单独搜 Red Dead Redemption 2 → 1174180）。
    所以拆成：别名全名 → 英文部分 → 全名 → 中文部分。
    """
    words = (name or "").split()
    ascii_run = " ".join(w for w in words if w.isascii() and w.strip())
    zh_run = "".join(w for w in words if not w.isascii() and w.strip())
    terms = []
    alias = ALIASES.get(norm_name(name))
    if alias:
        terms.append(alias)
    for t in (ascii_run, name, zh_run):
        t = (t or "").strip()
        if t and t not in terms:
            terms.append(t)
    return terms


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
def default_fetch(url: str) -> bytes:
    """默认取数函数。失败抛异常，由 lookup() 统一归类。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


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
            "error": ""}


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


def lookup(name: str, fetch=None) -> dict:
    """查一款游戏在 Steam 上的身份。

    返回 dict：
        status        ok / not_found / error
        appid         Steam appid（字符串，查不到为空）
        official_name 商店里的英文名
        name_zh       官方中文名，没有则为空串
        error         仅 status=error 时有意义

    注意 status=ok 但 name_zh 为空是**正常结果**：大量独立游戏在 Steam
    国区只有英文名。调用方要把「查到了但没中文名」与「没查到」区分开，
    否则这类游戏会被无限重试。
    """
    fetch = fetch or default_fetch
    res = {"status": STATUS_NOT_FOUND, "appid": "", "name_zh": "",
           "official_name": "", "error": ""}
    name = (name or "").strip()
    if not name:
        return res

    any_parsed = False
    last_error = ""
    for term in search_terms(name):
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
            return _ok(first.get("id"), official,
                       [first.get("_local"), official])
        hit = _best_match(term, items)
        if hit:
            official = (hit.get("name") or "").strip()
            return _ok(hit.get("id"), official, [official])

    # 所有候选词都解析失败才算网络错误；只要有一轮成功解析，
    # 那就是「确实没搜到」，属于 not_found，不该触发重试退避的 error 分支。
    if not any_parsed and last_error:
        res["status"] = STATUS_ERROR
        res["error"] = last_error
    return res


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
