# -*- coding: utf-8 -*-
"""
平台识别：Steam / Epic 的安装清单、卸载入口、安装根定位。

只做「读」——不删任何文件、不改任何游戏目录。卸载动作交给平台自己的
卸载器（Steam / Epic 的原生协议）或游戏自带的 unins000.exe；两者都识别
不出时只返回「打开安装目录」，由用户自行处理。

为什么不能走注册表卸载串：
    实测本机 5 款游戏（Split Fiction / 米塔 / DDLC / 幸福工厂 / Big Walk）
    在 HKLM 与 HKCU 的 Uninstall 键下 **0 条命中**。游戏普遍不登记卸载项，
    所以「读 UninstallString 然后执行」这条路对游戏基本无效，只能靠平台清单。

Steam 的数据来源（全部为本地文件，不联网）：
    注册表 HKCU\\Software\\Valve\\Steam 的 SteamPath
    <Steam>/steamapps/libraryfolders.vdf          每个库根一条 path
    <库>/steamapps/appmanifest_<appid>.acf        installdir / name / StateFlags

Epic 的数据来源：
    %ProgramData%\\Epic\\EpicGamesLauncher\\Data\\Manifests\\*.item
    JSON，含 DisplayName / InstallLocation / AppName（是哈希，不是 appid）
"""
import glob
import json
import os
import re
import sys

try:
    import winreg
except Exception:                       # 非 Windows 平台
    winreg = None

# StateFlags 里「已完整安装」的位。Steam 用位标记记录状态，
# 4 = StateFullyInstalled。未安装 / 更新中都不带这一位。
_STATE_FULLY_INSTALLED = 4

_EPIC_MANIFEST_DIRS = (
    r"%ProgramData%\Epic\EpicGamesLauncher\Data\Manifests",
    r"%ProgramData%\Epic\EpicGamesLauncher\Data\Manifests\Pending",
)


# ==================================================================
# 小工具
# ==================================================================
def _expand(p: str) -> str:
    return os.path.expandvars(os.path.expanduser((p or "").strip()))


def _kv_lines(text: str):
    """把 VDF / ACF 里的 `"key" "value"` 行逐个吐出来。

    只做逐行解析、不建树：我们要的字段都是扁平的，建一棵完整 VDF 树
    对这里没有收益，还会把「花括号 / 嵌套」的解析错误带进来。
    """
    for m in re.finditer(r'^\s*"([^"]*)"\s+"([^"]*)"\s*$', text, re.M):
        yield m.group(1), m.group(2)


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


# ==================================================================
# Steam
# ==================================================================
def _reg_value(hive, subkey: str, name: str):
    if winreg is None:
        return None
    try:
        with winreg.OpenKey(hive, subkey) as k:
            return winreg.QueryValueEx(k, name)[0]
    except Exception:
        return None


def steam_root() -> str:
    """Steam 安装根目录，取不到返回空串。

    注册表优先（最准），再退到两个常见路径 —— 有些机器 Steam 是绿色版，
    注册表里没有 SteamPath。
    """
    if winreg is not None:
        for hive, key in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                          (winreg.HKEY_LOCAL_MACHINE,
                           r"SOFTWARE\WOW6432Node\Valve\Steam")):
            for name in ("SteamPath", "InstallPath"):
                v = _reg_value(hive, key, name)
                if v and os.path.isdir(v):
                    return os.path.normpath(v)
    for p in (r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam",
              r"C:\Steam", r"D:\Steam", r"E:\Steam"):
        if os.path.isdir(p):
            return p
    return ""


def steam_libraries() -> list:
    """返回所有 steamapps 目录（含 libraryfolders.vdf 里登记的其它盘）。"""
    out = []
    seen = set()

    def push(p):
        # Windows 路径大小写不敏感：注册表里写的是 "c:/steam"，vdf 里是
        # "C:\\steam"，同一个库会被算成两条。去重键必须 normcase。
        p = os.path.normpath(p)
        k = os.path.normcase(p)
        if k not in seen and os.path.isdir(p):
            seen.add(k)
            out.append(p)

    root = steam_root()
    if root:
        base = os.path.join(root, "steamapps")
        push(base)
        vdf = os.path.join(base, "libraryfolders.vdf")
        text = _read_text(vdf)
        if text:
            for k, v in _kv_lines(text):
                # 新格式："path"  "D:\\SteamLibrary"
                # 旧格式："1"     "D:\\SteamLibrary"（值以盘符开头）
                cand = v if k == "path" else (v if re.match(r"^[A-Za-z]:[\\/]", v) else "")
                if not cand:
                    continue
                push(os.path.join(os.path.normpath(cand), "steamapps"))
    # 注册表缺失时兜底：扫常见目录，避免整条链路因一个键读不到就失效
    if not out:
        for p in (r"C:\Program Files (x86)\Steam\steamapps",
                  r"C:\Program Files\Steam\steamapps", r"C:\steam\steamapps"):
            push(p)
    return out


def steam_app(appid) -> dict:
    """按 appid 找 Steam 游戏。找不到返回 None。

    返回 {"appid","name","installdir","install_dir","library","installed","size"}
    即使游戏已卸载（没有 acf）也返回 None —— 没有清单就无从得知装到哪。
    """
    appid = str(appid or "").strip()
    if not appid.isdigit():
        return None
    for lib in steam_libraries():
        acf = os.path.join(lib, "appmanifest_%s.acf" % appid)
        if not os.path.isfile(acf):
            continue
        fields = dict(_kv_lines(_read_text(acf)))
        installdir = fields.get("installdir") or ""
        if not installdir:
            continue
        # acf 在 <lib>/steamapps 下，安装根是 <lib>/steamapps/common/<installdir>
        install_dir = os.path.join(lib, "common", installdir)
        try:
            flags = int(fields.get("StateFlags") or 0)
        except ValueError:
            flags = 0
        try:
            size = int(fields.get("SizeOnDisk") or 0)
        except ValueError:
            size = 0
        return {"appid": appid,
                "name": fields.get("name") or "",
                "installdir": installdir,
                "install_dir": os.path.normpath(install_dir),
                "library": os.path.normpath(lib),
                "installed": bool(flags & _STATE_FULLY_INSTALLED)
                             and os.path.isdir(install_dir),
                "size": size}
    return None


def steam_library_roots() -> list:
    """所有库的游戏安装父目录（.../steamapps/common），用于同名目录搜索。"""
    return [os.path.join(lib, "common") for lib in steam_libraries()
            if os.path.isdir(os.path.join(lib, "common"))]


# ==================================================================
# Epic
# ==================================================================
def epic_manifests() -> list:
    """所有 Epic 清单条目。读不到返回空列表（Epic 没装就是常态）。"""
    out = []
    for d in _EPIC_MANIFEST_DIRS:
        d = _expand(d)
        for p in glob.glob(os.path.join(d, "*.item")):
            try:
                with open(p, "r", encoding="utf-8-sig") as f:
                    data = json.load(f)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            loc = data.get("InstallLocation") or ""
            if not loc:
                continue
            out.append({"app_name": data.get("AppName") or "",
                        "display_name": data.get("DisplayName") or "",
                        "install_dir": os.path.normpath(loc),
                        "installed": os.path.isdir(loc)})
    return out


def epic_app(display_name: str) -> dict:
    """按显示名找 Epic 条目（Epic 的 AppName 是哈希，只能靠名字对）。"""
    from .gamemeta import norm_name
    q = norm_name(display_name or "")
    if not q:
        return None
    best = None
    for it in epic_manifests():
        c = norm_name(it.get("display_name") or "")
        if not c:
            continue
        if c == q:
            return it
        # 允许译名差异时靠包含关系兜底，但要求够长，避免 "save" 之类误配
        if len(q) >= 4 and (q in c or c in q):
            best = best or it
    return best


def epic_library_roots() -> list:
    """Epic 安装目录的父级（.../Epic Games），用于同名目录搜索。"""
    roots = []
    for it in epic_manifests():
        parent = os.path.dirname(it["install_dir"])
        if parent and os.path.isdir(parent) and parent not in roots:
            roots.append(parent)
    return roots


# ==================================================================
# 定位安装目录
# ==================================================================
def find_install_dir(name: str, appid: str = "", platform: str = "") -> dict:
    """尽力找出某款游戏的安装目录。

    返回 {"install_dir","source","confidence","name"}，找不到返回 {}。
    confidence: high（平台清单） / medium（同名目录） / ""
    """
    hit = steam_app(appid) if appid else None
    if hit and hit.get("install_dir"):
        return {"install_dir": hit["install_dir"], "source": "steam",
                "confidence": "high", "name": hit.get("name") or name,
                "installed": hit.get("installed", False),
                "size": hit.get("size", 0), "appid": appid,
                "installdir": hit.get("installdir") or ""}
    e = epic_app(name) if name else None
    if e and e.get("install_dir"):
        return {"install_dir": e["install_dir"], "source": "epic",
                "confidence": "high", "name": e.get("display_name") or name,
                "installed": e.get("installed", False), "size": 0,
                "app_name": e.get("app_name") or ""}
    # 没清单也没关系：游戏名同名目录扫一遍（medium，仅作候选）
    for root in steam_library_roots() + epic_library_roots():
        p = os.path.join(root, name)
        if name and os.path.isdir(p):
            return {"install_dir": os.path.normpath(p), "source": "guess",
                    "confidence": "medium", "name": name,
                    "installed": True, "size": 0}
    return {}


# ==================================================================
# 卸载动作
# ==================================================================
def uninstall_action(install_dir: str = "", appid: str = "",
                     app_name: str = "") -> dict:
    """决定「卸载」该怎么做。只返回动作，不执行。

    method:
        steam       唤 Steam 原生卸载框（steam://uninstall/<appid>）
        epic        唤 Epic 原生卸载框
        uninstaller 执行游戏自带的卸载程序（unins000.exe / uninstall.exe）
        open        只能打开安装目录，让用户自己处理
        none        连目录都找不到
    返回 {"method","target","label"}
    """
    if appid and str(appid).isdigit() and steam_app(appid):
        return {"method": "steam",
                "target": "steam://uninstall/%s" % appid,
                "label": "由 Steam 卸载"}
    if app_name:
        return {"method": "epic",
                "target": ("com.epicgames.launcher://apps/%s?action=uninstall"
                           % app_name),
                "label": "由 Epic 卸载"}
    if install_dir and os.path.isdir(install_dir):
        exe = find_uninstaller(install_dir)
        if exe:
            return {"method": "uninstaller", "target": exe,
                    "label": "运行游戏自带卸载程序"}
        return {"method": "open", "target": install_dir,
                "label": "打开安装目录"}
    return {"method": "none", "target": "", "label": "找不到安装目录"}


def find_uninstaller(install_dir: str) -> str:
    """在安装目录顶层找卸载程序。不递归 —— 子目录里的 uninstall.exe
    多半是某个组件的，不是整款游戏的卸载器，误跑会留下半卸状态。
    """
    if not install_dir or not os.path.isdir(install_dir):
        return ""
    names = ("unins000.exe", "unins001.exe", "uninstall.exe",
             "Uninstall.exe", "UNINSTALL.EXE", "uninst.exe")
    for n in names:
        p = os.path.join(install_dir, n)
        if os.path.isfile(p):
            return p
    return ""


# ==================================================================
# 路径迁移：旧存档路径 → 新安装位置
# ==================================================================
def relocate_path(old_path: str, old_install: str, new_install: str,
                  installdir: str = "") -> str:
    """把旧存档路径映射到新安装根下。映射不出来返回空串。

    两种输入：
      1. 知道旧安装根（游戏添加时存过 install_dir）：直接取相对后缀。
      2. 不知道旧安装根（老版本数据库）：在旧路径里找
         `steamapps/common/<installdir>` 这一段，取它后面的部分。

    只处理「存档就在安装目录里」的情况。存档在 AppData / 文档下的游戏
    根本不需要迁移，路径本来就不随安装位置变。
    """
    old_path = os.path.normpath(old_path or "")
    if not old_path or not new_install:
        return ""

    if old_install:
        oi = os.path.normpath(old_install)
        try:
            if os.path.commonpath([old_path.lower(), oi.lower()]) == oi.lower():
                suffix = os.path.relpath(old_path, oi)
                if suffix and not suffix.startswith(".."):
                    return os.path.normpath(os.path.join(new_install, suffix))
        except ValueError:
            pass

    # 没有旧安装根：靠 steamapps/common/<name> 这段定位
    parts = old_path.split(os.sep)
    lower = [p.lower() for p in parts]
    for i in range(len(parts) - 1):
        if lower[i] == "steamapps" and lower[i + 1] == "common" and i + 2 < len(parts):
            marker = parts[i + 2]                     # 旧 installdir
            if installdir and marker.lower() != installdir.lower():
                continue
            suffix_parts = parts[i + 3:]
            base = new_install
            # new_install 已含 installdir，suffix 只需接其后
            return os.path.normpath(os.path.join(base, *suffix_parts)) \
                if suffix_parts else os.path.normpath(base)
    return ""


def candidate_save_dirs(name: str, snapshot_rel_paths=()) -> list:
    """在没有平台清单时，猜存档目录可能在哪。

    只返回「确实存在、且里面有快照里同名文件」的目录，避免把一堆
    空目录丢给用户挑。snapshot_rel_paths 是快照里的相对路径集合。
    """
    want = {p.replace("\\", "/").split("/")[-1].lower()
            for p in snapshot_rel_paths if p}
    roots = []
    home = os.path.expanduser("~")
    for sub in (r"AppData\LocalLow", r"AppData\Local", r"AppData\Roaming",
                r"Documents\My Games", r"Documents", r"Saved Games"):
        roots.append(os.path.join(home, sub))
    out = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for first in _safe_listdir(root):
            p = os.path.join(root, first)
            if not os.path.isdir(p):
                continue
            # 两级：LocalLow 是 <工作室>/<游戏>，其它基本一层
            for cand in (p,) + tuple(os.path.join(p, s)
                                     for s in _safe_listdir(p)):
                if not os.path.isdir(cand):
                    continue
                if name and norm_loose(name) not in norm_loose(cand):
                    continue
                if want and not (want & top_files(cand)):
                    continue
                if cand not in out:
                    out.append(os.path.normpath(cand))
    return out[:8]


def norm_loose(s: str) -> str:
    """宽松归一：小写 + 只留字母数字（用于路径包含判断）。"""
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _safe_listdir(p: str) -> list:
    try:
        return sorted(os.listdir(p))
    except OSError:
        return []


def top_files(p: str) -> set:
    """顶层文件名（小写）。只看顶层：我们要判断的是「这个目录像不像
    那款游戏的存档目录」，深层比对交给快照指纹，不在这里做。
    """
    try:
        return {n.lower() for n in os.listdir(p)
                if os.path.isfile(os.path.join(p, n))}
    except OSError:
        return set()


def is_windows() -> bool:
    return sys.platform.startswith("win")
