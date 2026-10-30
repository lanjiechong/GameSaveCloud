# -*- coding: utf-8 -*-
"""
存档路径诊断与重新定位。

回答一个问题：这款游戏的存档目录不见了，它现在在哪？

分四层，从确定到模糊：
    1. 原路径还在            → ok，什么都不用做
    2. 平台清单算出新安装根   → relocate，置信度高，可直接应用
    3. 常见位置下的同名目录   → candidates，列给用户挑
    4. 快照里的文件名指纹     → candidates（参与第 3 层的过滤）
    全部失败                 → lost，让用户手动指定，绝不猜

为什么第 2 层靠得住：快照里存的是**相对路径**，只要知道新安装根，
旧存档在安装目录内的相对位置就能原样映射过去：
    D:\\Steam\\steamapps\\common\\MiSide\\Saves
  → 新根 = E:\\Games\\steamapps\\common\\MiSide
  → E:\\Games\\steamapps\\common\\MiSide\\Saves
这也是当初坚持存相对路径的收益，换盘、换库都不怕。

绝大部分游戏根本用不到这一层：存档在 %APPDATA%、%LOCALAPPDATA%、文档
下的游戏，路径由游戏名和工作室名决定，跟装到哪个盘无关，第 1 层直接命中。
"""
import os

from . import launcher


def missing_roots(game) -> list:
    """返回已经不存在的存档目录。"""
    return [p for p in (game.paths or []) if not os.path.isdir(p)]


def snapshot_filenames(vault, game_id: str, limit: int = 40) -> list:
    """取最新快照里的文件名（只取基名），用于判断「这个目录像不像它的存档」。"""
    try:
        snap = vault.latest_snapshot(game_id)
    except Exception:
        return []
    if not snap:
        return []
    out = []
    for e in snap.files[:limit]:
        name = (e.path or "").replace("\\", "/").split("/")[-1]
        if name and name not in out:
            out.append(name)
    return out


def diagnose(game, vault=None, appid: str = "") -> dict:
    """诊断存档路径。返回 dict，state 决定调用方怎么处理。

    state:
        ok          路径都还在
        relocate    已算出新的可用路径（layers 1-2），可直接应用
        candidates  找到几个候选目录，需要用户确认
        lost        找不到，需要用户手动指定
    """
    paths = list(game.paths or [])
    missing = missing_roots(game)
    if not missing:
        return {"state": "ok", "missing": [], "paths": paths,
                "install_dir": game.install_dir or "", "source": "",
                "confidence": "", "candidates": [], "note": ""}

    if not appid:
        try:
            meta = vault.get_meta(game.id) if vault is not None else None
            appid = (meta or {}).get("appid") or ""
        except Exception:
            appid = ""

    info = launcher.find_install_dir(game.name, appid, game.platform)
    new_install = info.get("install_dir") or ""
    installed = bool(info.get("installed"))
    installdir = info.get("installdir") or ""

    # 第 1-2 层：原路径 + 平台清单
    if new_install and os.path.isdir(new_install):
        mapped = []
        ok_all = True
        for p in paths:
            if os.path.isdir(p):
                mapped.append(p)
                continue
            np = launcher.relocate_path(p, game.install_dir or "",
                                        new_install, installdir)
            if np and os.path.isdir(np):
                mapped.append(np)
            else:
                ok_all = False
                break
        if ok_all and mapped != paths:
            return {"state": "relocate", "missing": missing, "paths": mapped,
                    "install_dir": new_install, "source": info.get("source", ""),
                    "confidence": info.get("confidence", ""), "candidates": [],
                    "note": "按%s安装清单定位到新位置" % _src_label(info)}

    # 第 3-4 层：常见位置下的同名目录 + 快照文件名指纹
    names = snapshot_filenames(vault, game.id) if vault is not None else []
    cands = launcher.candidate_save_dirs(game.name, names)
    if cands:
        return {"state": "candidates", "missing": missing, "paths": [],
                "install_dir": new_install, "source": info.get("source", ""),
                "confidence": "medium",
                "candidates": [{"path": c, "matched": len(
                    set(n.lower() for n in names) &
                    set(x.lower() for x in launcher.top_files(c)))}
                    for c in cands],
                "note": "找到 %d 个疑似目录" % len(cands)}

    # 都没找到：区分「游戏没装」和「存档真没了」，提示才有用
    if new_install and not installed:
        note = "游戏当前未安装，装回来之后点「找回存档」即可自动定位"
    elif new_install:
        note = "游戏已安装在 %s，但存档不在其中，可能已被游戏删除" % new_install
    else:
        note = "未找到安装位置，请手动指定存档目录"
    return {"state": "lost", "missing": missing, "paths": [],
            "install_dir": new_install, "source": info.get("source", ""),
            "confidence": "", "candidates": [], "note": note}


def _src_label(info: dict) -> str:
    return {"steam": "Steam ", "epic": "Epic ", "guess": ""}.get(
        info.get("source") or "", "")


def apply(vault, game, paths, install_dir=None) -> bool:
    """把诊断出的路径写进库。id 不变，历史快照原样接上。"""
    if not paths:
        return False
    if install_dir is None:
        install_dir = game.install_dir or ""
    vault.update_paths(game.id, paths, install_dir)
    game.paths = [os.path.abspath(p) for p in paths]
    game.install_dir = install_dir
    return True
