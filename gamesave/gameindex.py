# -*- coding: utf-8 -*-
r"""离线游戏资料索引。

数据来自 ``scripts/build_game_index.py`` 抓取的 Steam 畅销榜 Top100
（中英文名 / 发行商 / 类型 / 评价数 / 图标地址），产物
``ui/assets/gameindex.json`` 随发布包一起分发。

为什么要有这一层：
    网络是最不可靠的一环。索引命中时直接返回结果、**零网络请求** ——
    断网、被限流、商店改版都不影响列表显示中文名和图标。
    它也是「Steam 搜不到这款游戏」时的兜底：榜单内的游戏必定能认出来。

数据是元数据，不含任何图片二进制；商标素材仍然只在运行时从 CDN 取，
所以这个文件可以随仓库和发布包分发。
"""
import json
import os
import sys

from .gamemeta import STATUS_OK, name_match, norm_name, similar

_CACHE = None


def candidate_paths() -> list:
    """索引候选路径，按优先级从高到低。

    1. 打包目录 _MEIPASS/ui/assets/gameindex.json
    2. 开发目录 ui/assets/gameindex.json
    3. 用户目录 %LOCALAPPDATA%/GameSaveCloud/gameindex.json（手动补充）
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = []
    if getattr(sys, "_MEIPASS", None):
        out.append(os.path.join(sys._MEIPASS, "ui", "assets", "gameindex.json"))
    out.append(os.path.join(root, "ui", "assets", "gameindex.json"))
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    out.append(os.path.join(base, "GameSaveCloud", "gameindex.json"))
    return out


def load() -> dict:
    """读索引，返回 {appid: record}。任何一层缺失都静默跳过。

    高优先级路径先到先得：打包进 exe 的那份不会被开发目录
    或用户目录里的旧文件盖掉，用户目录只用来补缺。
    """
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    games = {}
    for p in candidate_paths():
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        for appid, rec in (data.get("games") or {}).items():
            appid = str(appid)
            if appid not in games and isinstance(rec, dict):
                games[appid] = rec
    _CACHE = games
    return _CACHE


def clear_cache():
    """丢掉内存缓存（测试与「手动换了索引文件」时用）。"""
    global _CACHE
    _CACHE = None


def set_cache(games: dict):
    """直接注入索引内容。测试用，避免依赖磁盘上的真实索引文件。"""
    global _CACHE
    _CACHE = {str(k): v for k, v in (games or {}).items()}


def _names(rec: dict) -> list:
    out = [rec.get("name_zh") or "", rec.get("name_en") or ""]
    out.extend(n for n in (rec.get("aliases") or []) if n)
    return [n for n in out if n]


def _match(games: dict, name: str, key: str):
    """在索引里找最像的一条，返回 (appid, record) 或 None。"""
    # 第一轮：归一化后完全相同。目录名常常就是英文名的去空格形式
    # （SplitFiction / Cyberpunk2077），这一轮能秒中且不会误判。
    for appid, rec in games.items():
        for cand in _names(rec):
            if norm_name(cand) == key:
                return appid, rec
    # 第二轮：互相包含 / 相似度，取分数最高的
    best = None
    for appid, rec in games.items():
        for cand in _names(rec):
            if name_match(name, cand) or name_match(cand, name):
                score = similar(key, norm_name(cand))
                if best is None or score > best[0]:
                    best = (score, appid, rec)
    return (best[1], best[2]) if best else None


def lookup(name: str):
    """按名字查索引。命中返回与 gamemeta.lookup 同构的 dict，否则 None。

    返回体带 ``source="index"``，便于日志区分「本地认出」和「联网查到」。
    """
    games = load()
    name = (name or "").strip()
    if not games or not name:
        return None
    key = norm_name(name)
    if not key:
        return None
    hit = _match(games, name, key)
    if not hit:
        return None
    appid, rec = hit
    return {
        "status": STATUS_OK,
        "appid": appid,
        "name_zh": rec.get("name_zh") or "",
        "official_name": rec.get("name_en") or "",
        "error": "",
        "source": "index",
    }


def entries() -> list:
    """按评价数降序返回索引里的全部条目（带 appid），供界面/调试用。"""
    rows = []
    for appid, rec in load().items():
        rows.append(dict(rec, appid=appid))
    rows.sort(key=lambda r: r.get("reviews") or 0, reverse=True)
    return rows


def stats() -> dict:
    games = load()
    paths = candidate_paths()
    used = next((p for p in paths if os.path.isfile(p)), "")
    return {"count": len(games), "path": used}
