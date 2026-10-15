# -*- coding: utf-8 -*-
"""创建 GitHub Release 并上传 exe 资产。token 从环境变量读，不打印。"""
import json
import os
import sys
import urllib.request

TOKEN = os.environ.get("GSC_GH_TOKEN", "")
REPO = "lanjiechong/GameSaveCloud"
TAG = "v1.0.0"
DIST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dist")

BODY = """## GameSaveCloud v1.0.0

本机游戏存档的备份、历史版本、回滚与多设备同步工具。**本地备份为主，云备份为可选项** —— 不配任何云端也能完整工作。

### 下载

| 文件 | 用途 |
|---|---|
| `GameSaveCloud.exe` | 图形界面版，双击即用（无控制台窗口） |
| `GameSaveCloud-CLI.exe` | 命令行版，供计划任务 / 脚本调用 |

两个均为 Windows 单文件，无需安装 Python，拷到任意 Windows 机器可直接运行。
数据存放在 `%LOCALAPPDATA%\\GameSaveCloud\\`。

### 核心特性

- **备份引擎**：分块存储 + zstd 压缩 + 内容寻址去重，跨版本共享未变化的块；chunk 级断点续传
- **历史与回滚**：快照清单、内容查看、逐文件 diff、一键回滚（回滚前自动留底）；默认保留最近 30 个快照
- **云端同步（可选）**：本地文件夹 / WebDAV 两种后端；本地与云端同时改动时弹窗交人决策（保留本地 / 使用云端 / 两边各存一份），绝不静默二选一
- **调度**：定时自动备份、托盘常驻、可选开机自启
- **游戏图标**：内置 Steam 全球周销量榜 Top100 + 常青经典 93 款真实 logo，断网首启即有图

### 质量保障

- 47 个单元测试覆盖引擎层（分块去重 / 快照 / 同步 / 冲突策略）
- GUI 冒烟测试（离屏）：后台线程 SQLite、8 线程并发读、对话框构造、本地/云端两条同步链路
- 危险操作（回滚、覆盖、删除）一律二次确认，默认取消

### 从源码构建

```bash
pyinstaller GameSaveCloud.spec --noconfirm
```

完整变更记录见 [CHANGELOG.md](https://github.com/lanjiechong/GameSaveCloud/blob/main/CHANGELOG.md)。
"""


def api(url, method="GET", data=None, headers=None, timeout=120):
    h = {"Authorization": "token " + TOKEN, "Accept": "application/vnd.github+json"}
    if headers:
        h.update(headers)
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def upload(upload_url, path):
    name = os.path.basename(path)
    url = upload_url.split("{")[0] + "?name=" + name
    with open(path, "rb") as f:
        payload = f.read()
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Authorization": "token " + TOKEN,
                 "Content-Type": "application/octet-stream",
                 "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        d = json.loads(r.read().decode("utf-8"))
    return name, d.get("size"), d.get("state")


def main():
    if not TOKEN:
        print("ERROR: GSC_GH_TOKEN 未设置")
        return 1
    # 已存在则复用
    try:
        rel = api("https://api.github.com/repos/%s/releases/tags/%s" % (REPO, TAG))
        print("Release 已存在，复用 id=%s" % rel.get("id"))
    except urllib.error.HTTPError as e:
        if e.code != 404:
            print("查询失败: HTTP %s" % e.code)
            return 1
        rel = api("https://api.github.com/repos/%s/releases" % REPO, "POST", {
            "tag_name": TAG,
            "name": "GameSaveCloud v1.0.0 —— 游戏存档备份与同步",
            "body": BODY,
            "draft": False,
            "prerelease": False,
        })
        print("Release 已创建 id=%s" % rel.get("id"))

    up = rel.get("upload_url")
    for f in ("GameSaveCloud.exe", "GameSaveCloud-CLI.exe"):
        p = os.path.join(DIST, f)
        if not os.path.isfile(p):
            print("缺少文件: %s" % p)
            return 1
        name, size, state = upload(up, p)
        print("已上传 %s  %.1f MB  state=%s" % (name, size / 1048576.0, state))
    print("DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
