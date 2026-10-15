# -*- coding: utf-8 -*-
"""构建内置 logo 图标包（维护工具，非运行时代码）。

数据来源：
    1. Steam 商店「全球销量榜」前 100（store.steampowered.com 搜索页
       filter=globaltopsellers，抓取时需翻墙或代理，见 README）
    2. 常青经典补充清单（SUPPLEMENT，当前不在周榜但装机量极大的游戏）

产物（ui/assets/logos/）：
    <appid>.png    96x96 圆角方块图标，烘焙规则与 ui/logos.py 的
                   _to_tile 完全一致（contain/cover + 圆角裁剪），
                   保证内置图标与联网取到的图标视觉相同
    logo_pack.json  appid -> 可匹配名称列表（官方中文名/英文名/俗名）

用法：
    python scripts/fetch_top_logos.py [--ids top_ids.txt]

依赖：Pillow（只在构建时需要，运行时不需要）。
"""
import argparse
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from PIL import Image, ImageChops, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT_DIR = os.path.join(ROOT, "ui", "assets", "logos")
PACK_JSON = os.path.join(OUT_DIR, "logo_pack.json")

TILE = 96
RADIUS = TILE * 0.22          # 与 ui/logos.py _to_tile 保持一致
UA = "Mozilla/5.0 (compatible; GameSaveCloud/1.0)"
TIMEOUT = 15
RETRIES = 2
DELAY = 0.12                  # 请求间隔，避免被 Steam 限流

# 常青经典补充：appid -> 中文俗名/别称（官方名由 appdetails 自动补）
# 注意：appid 以 appdetails 返回为准，构建日志里会逐个打印真实游戏名，
# 发现记错就及时修正（记错的别名比没有别名更糟 —— 会把 A 游戏的图标
# 安到 B 游戏头上）。
SUPPLEMENT = {
    271590: ["给他爱5", "GTA5", "侠盗猎车手5", "侠盗猎车手V", "GTA V"],
    292030: ["巫师3", "巫师3狂猎", "The Witcher 3"],
    413150: ["星露谷物语", "星露谷", "Stardew Valley"],
    105600: ["泰拉瑞亚", "Terraria"],
    1145360: ["哈迪斯", "Hades"],
    367520: ["空洞骑士", "Hollow Knight"],
    504230: ["蔚蓝", "Celeste"],
    588650: ["死亡细胞", "Dead Cells"],
    1145350: ["哈迪斯2", "Hades II"],
    2379780: ["小丑牌", "Balatro"],
    1868140: ["潜水员戴夫", "Dave the Diver"],
    1097150: ["糖豆人", "Fall Guys"],
    945360: ["太空狼人杀", "我们之中", "Among Us"],
    892970: ["英灵神殿", "Valheim"],
    526870: ["幸福工厂", "Satisfactory"],
    427520: ["异星工厂", "Factorio"],
    294100: ["边缘世界", "RimWorld"],
    457140: ["缺氧", "Oxygen Not Included"],
    255710: ["城市天际线", "Cities: Skylines"],
    613100: ["房产达人", "House Flipper"],
    1593500: ["战神", "战神4", "God of War"],
    1599340: ["对马岛之魂", "Ghost of Tsushima"],
    1361210: ["战锤40K：暗潮", "暗潮", "Warhammer 40,000: Darktide"],
    524220: ["尼尔：机械纪元", "NieR: Automata"],
    2050650: ["生化危机4", "生化危机4重制版", "Resident Evil 4"],
    1190460: ["死亡搁浅", "Death Stranding"],
    1222670: ["模拟人生4", "The Sims 4"],
    374320: ["黑暗之魂3", "魂3", "Dark Souls III"],
    570940: ["黑暗之魂", "黑暗之魂重制版", "Dark Souls Remastered"],
    1627720: ["匹诺曹的谎言", "Lies of P"],
    1448440: ["卧龙：苍天陨落", "Wo Long: Fallen Dynasty"],
    1326470: ["森林之子", "Sons of the Forest"],
    924970: ["喋血复仇", "Back 4 Blood"],
    990080: ["霍格沃茨之遗", "Hogwarts Legacy"],
}

# appdetails 的 type 字段里这些类别不会给单机游戏做图标，
# 进来了也是白下载（DLC 多半复用本体图，硬件根本没有图）。
SKIP_TYPES = {"dlc", "hardware", "music", "video", "advertising", "series"}

# 周榜里认得出的热门游戏的俗名（认不出的交给 appdetails 官方名）
CHART_ALIASES = {
    578080: ["吃鸡", "绝地求生", "PUBG"],
    1174180: ["荒野大镖客2", "大表哥2", "RDR2", "Red Dead Redemption II"],
    1091500: ["赛博朋克2077"],
    2767030: ["漫威争锋", "Marvel Rivals"],
    1623730: ["幻兽帕鲁", "Palworld"],
    553850: ["地狱潜者2", "绝地潜兵2", "Helldivers 2"],
    1364780: ["街霸6", "街头霸王6", "Street Fighter 6"],
    1085660: ["命运2", "Destiny 2"],
    814380: ["只狼", "只狼：影逝二度", "Sekiro"],
    1245620: ["老头环", "艾尔登法环"],
    1086940: ["博德之门3", "Baldur's Gate 3"],
    431960: ["动态壁纸", "壁纸引擎", "Wallpaper Engine"],
    730: ["CS2", "CSGO2"],
    570: ["刀塔2", "DOTA2"],
    1172470: ["Apex英雄", "Apex Legends"],
    230410: ["星际战甲", "Warframe"],
    3115160: [],
}


# ---------------------------------------------------------------
# 网络
# ---------------------------------------------------------------
def http_json(url, retries=RETRIES):
    raw = http_raw(url, retries)
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return None


def http_raw(url, retries=RETRIES):
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if i >= retries:
                return None
        except Exception:
            if i >= retries:
                return None
        time.sleep(0.6 * (i + 1))
    return None


def appdetails(appid, lang, retries=3):
    """返回 (name, type)；失败返回 (None, None)。"""
    url = ("https://store.steampowered.com/api/appdetails"
           "?appids=%s&l=%s&filters=basic" % (appid, lang))
    d = http_json(url, retries=retries)
    if not d:
        return None, None
    node = d.get(str(appid))
    if not node or not node.get("success"):
        return None, None
    data = node.get("data") or {}
    return data.get("name"), data.get("type")


# ---------------------------------------------------------------
# 烘焙：与 ui/logos.py 的 _to_tile 同一套规则
# ---------------------------------------------------------------
def bake_tile(src_bytes, dst_path):
    """任意来源图片 -> 96x96 圆角方块 PNG。

    contain（有透明底）：等比缩放进 96 内居中，四周留透明。
    cover（不透明）：等比放大铺满后居中裁切。
    判定方式与 Qt 版一致：只看图片正中心那个像素透不透明。
    """
    try:
        im = Image.open(io.BytesIO(src_bytes))
        im = im.convert("RGBA")
    except Exception:
        return False
    w, h = im.size
    if w < 8 or h < 8:
        return False
    center_alpha = im.getpixel((w // 2, h // 2))[3]
    has_alpha = center_alpha != 255
    if has_alpha:
        scale = min(TILE / w, TILE / h)
        nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
        small = im.resize((nw, nh), Image.LANCZOS)
        canvas = Image.new("RGBA", (TILE, TILE), (0, 0, 0, 0))
        canvas.paste(small, ((TILE - nw) // 2, (TILE - nh) // 2), small)
    else:
        scale = max(TILE / w, TILE / h)
        nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
        big = im.resize((nw, nh), Image.LANCZOS)
        left, top = (nw - TILE) // 2, (nh - TILE) // 2
        canvas = big.crop((left, top, left + TILE, top + TILE)).convert("RGBA")
    # 圆角裁剪：把图标自身 alpha 与圆角 mask 相乘，保留内部镂空
    mask = Image.new("L", (TILE, TILE), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, TILE - 1, TILE - 1], radius=RADIUS, fill=255)
    canvas.putalpha(ImageChops.multiply(canvas.getchannel("A"), mask))
    canvas.save(dst_path, "PNG", optimize=True)
    return True


def fetch_logo(appid, dst_path):
    """取 logo.png，取不到退 header.jpg。返回 (成功?, 来源)。"""
    for name in ("logo.png", "header.jpg"):
        url = ("https://cdn.akamai.steamstatic.com/steam/apps/%s/%s"
               % (appid, name))
        data = http_raw(url, retries=1)
        if data and bake_tile(data, dst_path):
            return True, name
        time.sleep(DELAY)
    return False, None


# ---------------------------------------------------------------
# 名称匹配（与 ui/logos.py 同规则，构建工具不依赖 PySide6 故单独实现）
# ---------------------------------------------------------------
def norm(s):
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def similar(a, b):
    import difflib
    return difflib.SequenceMatcher(None, a, b).ratio()


def name_match(query, candidate):
    q, c = norm(query), norm(candidate)
    if not q or not c:
        return False
    if q == c:
        return True
    if len(q) >= 3 and len(c) >= 3 and (q in c or c in q):
        return True
    return similar(q, c) >= 0.62


def ascii_part(name):
    """取名字里连续的英文部分（扫描器的游戏名是「中文 英文」组合名）。"""
    words = [w for w in (name or "").split() if w.isascii() and w.strip()]
    return " ".join(words).strip()


def zh_part(name):
    words = [w for w in (name or "").split() if not w.isascii() and w.strip()]
    return "".join(words).strip()


def storesearch_appid(term):
    url = ("https://store.steampowered.com/api/storesearch/"
           "?term=%s&cc=cn&l=schinese" % urllib.parse.quote(term))
    d = http_json(url)
    if not d:
        return None
    best = None
    for it in d.get("items", []):
        if it.get("type") and it.get("type") != "app":
            continue
        cand = it.get("name") or ""
        if name_match(term, cand):
            s = similar(norm(term), norm(cand))
            if best is None or s > best[0]:
                best = (s, it.get("id"))
    return best[1] if best else None


def scanner_game_names():
    """从 gamesave/scanner.py 的 KNOWN_GAMES 提取游戏名。"""
    path = os.path.join(ROOT, "gamesave", "scanner.py")
    src = open(path, encoding="utf-8").read()
    # 条目形如 ("游戏名 中文 English", "平台", [...], "根")
    return re.findall(r'^\s*\("([^"]+)",\s*"(?:Steam|Rockstar|Mojang|GOG|Epic|'
                      r'Ubisoft|EA|PS模拟|Xbox|Switch|其他)[^"]*"', src,
                      re.MULTILINE)


# ---------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default=None,
                    help="销量榜 appid 列表文件（每行一个）")
    ap.add_argument("--no-resume", action="store_true",
                    help="默认断点续传：已有 PNG 的条目直接沿用旧映射，"
                         "加此参数强制全部重新下载")
    args = ap.parse_args()

    chart_ids = []
    if args.ids and os.path.isfile(args.ids):
        chart_ids = [l.strip() for l in open(args.ids, encoding="utf-8")
                     if l.strip()]
    all_ids = list(dict.fromkeys(chart_ids + [str(k) for k in SUPPLEMENT]))
    print("待处理 appid：%d（周榜 %d + 补充 %d）"
          % (len(all_ids), len(chart_ids), len(SUPPLEMENT)))

    os.makedirs(OUT_DIR, exist_ok=True)
    games = {}          # appid -> {"names": [...]}
    ok_logo, fail_logo = 0, []

    # 断点续传：读旧 JSON，已有 PNG 且旧映射里有的条目直接沿用，
    # 不重复发请求（Steam 对高频请求会限流，重跑时只补缺的）
    old_games = {}
    if not args.no_resume and os.path.isfile(PACK_JSON):
        try:
            with open(PACK_JSON, encoding="utf-8") as f:
                old_games = json.load(f).get("games") or {}
        except Exception:
            old_games = {}

    for i, appid in enumerate(all_ids, 1):
        dst = os.path.join(OUT_DIR, "%s.png" % appid)
        if appid in old_games and os.path.isfile(dst):
            games[appid] = old_games[appid]
            ok_logo += 1
            continue

        name_zh, type_zh = appdetails(appid, "schinese")
        time.sleep(DELAY)
        name_en, _ = appdetails(appid, "english")
        time.sleep(DELAY)
        if not name_zh and not name_en:
            print("[%3d/%d] %-9s ✗ appdetails 失败，跳过"
                  % (i, len(all_ids), appid))
            fail_logo.append(appid)
            continue
        if (type_zh or "").lower() in SKIP_TYPES:
            print("[%3d/%d] %-9s - 跳过（type=%s）%s"
                  % (i, len(all_ids), appid, type_zh, name_zh))
            continue
        primary = name_zh or name_en
        names = []
        for n in (name_zh, name_en):
            if n and norm(n) not in [norm(x) for x in names]:
                names.append(n)
        for a in (SUPPLEMENT.get(int(appid), []) +
                  CHART_ALIASES.get(int(appid), [])):
            if norm(a) not in [norm(x) for x in names]:
                names.append(a)
        games[appid] = {"names": names, "type": type_zh or ""}

        ok, src = fetch_logo(appid, dst)
        flag = "✓" if ok else "✗"
        print("[%3d/%d] %-9s %s %s%s  |  %s"
              % (i, len(all_ids), appid, flag,
                 primary, (" [%s]" % type_zh) if type_zh else "",
                 src or ""))
        if ok:
            ok_logo += 1
        else:
            fail_logo.append(appid)
            if os.path.isfile(dst):
                os.remove(dst)

    # 扫描器的游戏名 -> 若能解析到榜上 appid，也加成别名
    print("\n解析扫描器游戏名 -> appid 映射…")
    hit = 0
    for gname in scanner_game_names():
        gname = gname.strip()
        if not gname:
            continue
        appid = None
        for term in (ascii_part(gname), gname, zh_part(gname)):
            if not term:
                continue
            appid = storesearch_appid(term)
            time.sleep(DELAY)
            if appid:
                break
        if appid and str(appid) in games:
            names = games[str(appid)]["names"]
            if norm(gname) not in [norm(x) for x in names]:
                names.append(gname)
                hit += 1
    print("扫描器名命中并追加别名：%d 个" % hit)

    # 没有 PNG 的条目不进 JSON —— 留在表里只会让 _pack_lookup
    # 命中一个取不到图的 appid，白白挡掉后面的 BUILTIN / 联网层级。
    for a in [k for k in games
              if not os.path.isfile(os.path.join(OUT_DIR, "%s.png" % k))]:
        del games[a]

    # 写 JSON
    pack = {
        "generated_at": time.strftime("%Y-%m-%d"),
        "source": "Steam 全球周销量榜 Top100 + 常青经典补充 + 扫描器别名",
        "tile": TILE,
        "games": games,
    }
    with open(PACK_JSON, "w", encoding="utf-8") as f:
        json.dump(pack, f, ensure_ascii=False, indent=1)

    total = sum(os.path.getsize(os.path.join(OUT_DIR, fn))
                for fn in os.listdir(OUT_DIR) if fn.endswith(".png"))
    print("\n===== 完成 =====")
    print("图标包条目：%d，成功下载烘焙：%d，失败：%d"
          % (len(games), ok_logo, len(fail_logo)))
    if fail_logo:
        print("失败 appid：%s" % ", ".join(fail_logo))
    print("PNG 总体积：%.2f MB（%d 个文件）"
          % (total / 1048576.0, ok_logo))
    print("输出目录：%s" % OUT_DIR)


if __name__ == "__main__":
    main()
