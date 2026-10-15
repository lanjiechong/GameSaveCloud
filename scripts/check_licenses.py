# -*- coding: utf-8 -*-
"""依赖许可证自查（发布前跑一遍）。

两种模式，自动选择：
    1. 装了 pip-licenses → 调它输出完整清单（含间接依赖）：
           pip install pip-licenses
           python scripts/check_licenses.py
    2. 没装 → 用标准库 importlib.metadata 直接读已安装包的元数据，
       零额外依赖也能列出「包 / 版本 / 许可证 / 主页」，
       并对 GPL/AGPL 这类强 Copyleft 许可证给出醒目警告。

本项目的直接依赖见 requirements.txt：
    PySide6    LGPL-3.0（动态链接，详见 THIRD_PARTY_NOTICES.md）
    zstandard  BSD-3-Clause（可选加速）
"""
import os
import subprocess
import sys

# 与商业/闭源分发可能冲突的强 Copyleft 关键字（LGPL/MPL 不在内，
# 它们允许动态链接使用，但清单里会照常列出，由人确认使用方式）。
STRONG_COPYLEFT = ("GPL-1", "GPL-2", "GPL-3", "AGPL",
                   "GNU General Public License")


def via_pip_licenses() -> bool:
    """优先用 pip-licenses（信息最全）。没装就返回 False 走内置实现。"""
    try:
        r = subprocess.run(
            [sys.executable, "-m", "piplicenses",
             "--from=mixed", "--format=plain", "--order=name"],
            capture_output=True, text=True, timeout=60)
    except Exception:
        return False
    if r.returncode != 0:
        return False
    print(r.stdout)
    return True


def via_stdlib():
    """标准库兜底：读当前环境里 requirements.txt 涉及包的元数据。"""
    import importlib.metadata as md

    # 直接依赖 + 打包时必然进入产物的大包；间接依赖也一并列出
    wanted = set()
    req = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "requirements.txt")
    if os.path.isfile(req):
        for line in open(req, encoding="utf-8"):
            line = line.split("#", 1)[0].strip()
            if line:
                wanted.add(line.split(">=")[0].split("==")[0].strip())
    wanted.update({"PySide6", "zstandard", "shiboken6", "pip", "setuptools"})

    rows, warnings_ = [], []
    for dist in sorted(md.distributions(), key=lambda d: (
            (d.metadata.get("Name") or "").lower())):
        name = dist.metadata.get("Name") or ""
        if name not in wanted and not any(
                name.lower().startswith(p.lower()) for p in
                ("pyside6", "shiboken", "zstandard", "qt")):
            continue
        lic = dist.metadata.get("License") or \
            _license_from_classifiers(dist) or "(元数据未声明)"
        home = dist.metadata.get("Home-page") or \
            (dist.metadata.get_all("Project-URL") or [""])[0]
        rows.append((name, dist.version, lic, home))
        if any(k in lic for k in STRONG_COPYLEFT):
            warnings_.append("    !! %s %s 许可证为 %s —— 强 Copyleft，"
                             "分发前必须确认兼容性" % (name, dist.version, lic))

    print("%-28s %-14s %-34s %s" % ("包名", "版本", "许可证", "主页"))
    print("-" * 110)
    for name, ver, lic, home in rows:
        print("%-28s %-14s %-34s %s" % (name, ver, lic[:34], home[:60]))
    print()
    if warnings_:
        print("发现需要人工确认的强 Copyleft 许可证：")
        print("\n".join(warnings_))
        return 1
    print("未发现 GPL/AGPL 类强 Copyleft 直接依赖。")
    print("注：PySide6 为 LGPL-3.0，动态链接的合规义务见 "
          "THIRD_PARTY_NOTICES.md。")
    return 0


def _license_from_classifiers(dist) -> str:
    for c in dist.metadata.get_all("Classifier") or []:
        if c.startswith("License ::"):
            return c.split("::")[-1].strip()
    return ""


if __name__ == "__main__":
    print("== GameSaveCloud 依赖许可证清单 ==\n")
    if not via_pip_licenses():
        print("（未安装 pip-licenses，使用标准库模式；"
              "要更全的间接依赖清单可 pip install pip-licenses 后重跑）\n")
        sys.exit(via_stdlib())
