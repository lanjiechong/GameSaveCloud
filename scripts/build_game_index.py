# -*- coding: utf-8 -*-
"""构建离线游戏资料索引（维护工具，产物随包分发）。

为什么要有这个文件：
    Steam 的公开接口不是随时可达的 —— 断网、限流、商店抽风都会让
    「游戏名 → 中文名 + 图标」这条链路哑火，列表里只能看到光秃秃的
    目录名。把畅销榜前 100 款的资料固化进安装包，任何情况下都能
    立刻显示中英文名、发行商、类型和图标地址。

产物：ui/assets/gameindex.json
    这是**元数据**（名称 / 发行商 / 类型 / 评价数 / 图标 URL），
    不含任何图片二进制。商标素材仍然只在运行时从 CDN 取，
    所以它可以随仓库和发布包分发，不受 logo 的合规限制。

选榜规则：
    抓 Steam「畅销榜」前 100（当前卖得动的），按累计评价数
    （recommendations.total，是最接近历史销量的公开数字）降序排列。
    周榜只看当下，评价数才代表「经典且畅销」—— 新游评价少会自动沉底，
    长期霸榜的老游戏浮上来。

为什么要抓两遍榜单：
    同一个榜单分别用 l=english / l=schinese 各抓一次，就能拿到
    「appid → 英文名」和「appid → 中文名」两张表，只有 2 次请求。
    否则每个游戏都要额外查一次 appdetails 才知道英文名，请求量翻倍，
    很容易被 Steam 限流。

用法：
    python scripts/build_game_index.py             # 断点续传
    python scripts/build_game_index.py --refresh   # 全部重抓
    python scripts/build_game_index.py --limit 100

依赖：仅标准库。
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "ui", "assets", "gameindex.json")
CACHE = os.path.join(ROOT, "build", "gameindex_cache.json")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) GameSaveCloud/1.3"
TIMEOUT = 25
DELAY = 0.25                 # 请求间隔；appdetails 有 200 次/5 分钟的限制

CHART_URL = ("https://store.steampowered.com/search/results/"
             "?query&start=%d&count=100&infinite=1"
             "&filter=topsellers&cc=cn&l=%s")
FILTERS = ("basic,developers,publishers,genres,recommendations,"
           "release_date,header_image")
DETAILS_URL = ("https://store.steampowered.com/api/appdetails"
               "?appids=%s&l=schinese&filters=" + FILTERS)

# 这些 type 不是可玩的单机/联机游戏，资料对列表没意义
SKIP_TYPES = {"dlc", "hardware", "music", "video", "advertising",
              "series", "demo", "mod"}


# ---------------------------------------------------------------
# 网络
# ---------------------------------------------------------------
def http_raw(url, retries=3):
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
            # 429/503：Steam 限流，退得久一点
            time.sleep((3.0 if e.code in (429, 503) else 1.0) * (i + 1))
        except Exception:
            if i >= retries:
                return None
            time.sleep(1.0 * (i + 1))
    return None


def http_json(url, retries=3):
    raw = http_raw(url, retries)
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return None


# ---------------------------------------------------------------
# 抓榜
# ---------------------------------------------------------------
def chart_pairs(lang, pages=1):
    """抓畅销榜，返回 [(appid, 榜单标题)]，顺序即榜单位次。

    抓多页是为了拿到足够的候选：周榜里混着刚上架、评价数为 0 的新游，
    只抓一页的话过滤完凑不满 100 条。
    """
    out = []
    for p in range(pages):
        d = http_json(CHART_URL % (p * 100, lang))
        if not d:
            print("  [warn] 榜单抓取失败：l=%s start=%d" % (lang, p * 100))
            continue
        html = d.get("results_html") or ""
        ids = re.findall(r'data-ds-appid="(\d+)"', html)
        titles = [t.strip() for t in re.findall(r'class="title">([^<]+)<', html)]
        if len(ids) != len(titles):
            print("  [warn] appid 与标题数量不一致（%d vs %d），按较短的对齐"
                  % (len(ids), len(titles)))
        out += list(zip(ids, titles))
        time.sleep(DELAY)
    return list(dict.fromkeys(out))


def details(appid):
    """取一款游戏的详情；失败或类型不符返回 None。"""
    d = http_json(DETAILS_URL % appid)
    if not d:
        return None
    node = d.get(str(appid)) or {}
    data = node.get("data")
    if not node.get("success") or not data:
        return None
    if (data.get("type") or "").lower() in SKIP_TYPES:
        return None
    return data


# ---------------------------------------------------------------
# 组装
# ---------------------------------------------------------------
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def has_cjk(text):
    return bool(_CJK.search(text or ""))


def build_record(data, title_en, title_zh, extra_names):
    """把 appdetails 的原始数据压成索引条目。"""
    api_name = (data.get("name") or "").strip()
    if has_cjk(api_name):
        name_zh, name_en = api_name, title_en
    else:
        name_zh, name_en = title_zh, api_name or title_en
    # 榜单标题可能被截断（「Call of Duty®: Modern Warfare® 4」），
    # 以 appdetails 的官方名为准，标题只作别名补充
    aliases = []
    for n in (title_en, title_zh) + tuple(extra_names or ()):
        n = (n or "").strip()
        if n and n not in (name_zh, name_en) and n not in aliases:
            aliases.append(n)
    return {
        "name_en": name_en or "",
        "name_zh": name_zh or "",
        "type": data.get("type") or "game",
        "developers": data.get("developers") or [],
        "publishers": data.get("publishers") or [],
        "genres": [g.get("description") for g in (data.get("genres") or [])
                   if g.get("description")],
        "release": ((data.get("release_date") or {}).get("date") or ""),
        "reviews": ((data.get("recommendations") or {}).get("total") or 0),
        "header": data.get("header_image") or "",
        "aliases": aliases,
    }


def load_cache():
    try:
        with open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cache(cache):
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    tmp = CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CACHE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=100,
                    help="保留多少条（按评价数降序），默认 100")
    ap.add_argument("--pages", type=int, default=2,
                    help="抓几页榜单（每页 100）。周榜混着刚上架的新游，"
                         "只抓一页过滤完凑不满 100 条。默认 2")
    ap.add_argument("--min-reviews", type=int, default=1,
                    help="评价数低于此值的条目视为新游，不参与排名。默认 1")
    ap.add_argument("--refresh", action="store_true",
                    help="忽略缓存，全部重新抓")
    args = ap.parse_args()

    print("1/3 抓畅销榜（中英双语，各 %d 页）…" % args.pages)
    en = chart_pairs("english", args.pages)
    zh = chart_pairs("schinese", args.pages)
    en_map = dict(en)
    zh_map = dict(zh)
    ids = list(dict.fromkeys([a for a, _ in en] + [a for a, _ in zh]))
    print("    英文榜 %d 条 / 中文榜 %d 条 / 合并 %d 个 appid"
          % (len(en), len(zh), len(ids)))
    if not ids:
        print("ERROR: 榜单为空，检查网络")
        return 1

    print("2/3 逐个取详情（%d 个，约 %.0f 秒）…" % (len(ids), len(ids) * 0.9))
    cache = {} if args.refresh else load_cache()
    games = {}
    done = 0
    for i, appid in enumerate(ids, 1):
        if appid in cache:
            games[appid] = cache[appid]
            done += 1
            print("[%3d/%d] %-9s 缓存" % (i, len(ids), appid))
            continue
        data = details(appid)
        time.sleep(DELAY)
        if not data:
            print("[%3d/%d] %-9s ✗ 跳过（取不到或无资料）" % (i, len(ids), appid))
            continue
        rec = build_record(data, en_map.get(appid, ""), zh_map.get(appid, ""), ())
        games[appid] = rec
        cache[appid] = rec
        done += 1
        print("[%3d/%d] %-9s %s | 评价 %s"
              % (i, len(ids), appid, rec["name_zh"] or rec["name_en"],
                 "{:,}".format(rec["reviews"])))
        if i % 20 == 0:
            save_cache(cache)

    save_cache(cache)
    print("    成功 %d 个" % done)

    print("3/3 按评价数排序，取前 %d …" % args.limit)
    eligible = [(a, r) for a, r in games.items()
                if (r.get("reviews") or 0) >= args.min_reviews]
    eligible.sort(key=lambda kv: kv[1]["reviews"], reverse=True)
    top = eligible[:args.limit]
    if len(top) < args.limit:
        print("    [warn] 只有 %d 条满足条件（评价数 >= %d），目标 %d 条"
              % (len(top), args.min_reviews, args.limit))
    out = {
        "generated_at": time.strftime("%Y-%m-%d"),
        "source": "Steam 畅销榜前 %d 个候选（中英双语标题）+ appdetails 详情，"
                  "剔除评价数 < %d 的新游后按累计评价数降序取前 %d"
                  % (len(ids), args.min_reviews, args.limit),
        "count": len(top),
        "games": {appid: rec for appid, rec in top},
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    os.replace(tmp, OUT)
    size = os.path.getsize(OUT)
    print("    已写入 %s（%d 条，%.1f KB）" % (OUT, len(top), size / 1024.0))
    print("    评价数区间：%s ~ %s"
          % ("{:,}".format(top[-1][1]["reviews"]) if top else "-",
             "{:,}".format(top[0][1]["reviews"]) if top else "-"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
