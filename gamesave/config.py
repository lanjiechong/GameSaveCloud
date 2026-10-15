# -*- coding: utf-8 -*-
"""
应用配置。存 vault/config.json，跨平台。

默认值按平台选合理的家目录，用户可在设置里改。
"""
import json
import os
import sys

DEFAULT_VAULT_NAME = "GameSaveVault"
DEFAULT_REMOTE_NAME = "GameSaveCloud"


def default_base() -> str:
    if sys.platform.startswith("win"):
        return os.path.join(os.environ.get("LOCALAPPDATA",
                                           os.path.expanduser("~")), "GameSaveCloud")
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library",
                            "Application Support", "GameSaveCloud")
    return os.path.join(os.path.expanduser("~"), ".gamesavecloud")


class Config:
    def __init__(self, base: str = None):
        self.base = base or default_base()
        os.makedirs(self.base, exist_ok=True)
        self.path = os.path.join(self.base, "config.json")
        self.data = {
            "vault_root": os.path.join(self.base, DEFAULT_VAULT_NAME),
            "remote": {
                # 云端同步总开关。默认 False —— 本应用以本地备份为主，
                # 只有用户明确配置了云端服务才打开。关闭时所有远端
                # （快照清单 / chunk 上传下载 / 裁剪）一律不碰。
                "enabled": False,
                "type": "local_folder",            # local_folder | webdav
                "path": os.path.join(self.base, DEFAULT_REMOTE_NAME),
                "url": "",
                "user": "",
                "password": "",
            },
            "schedule": {
                "enabled": False,
                "interval_minutes": 60,
                "silent": True,                    # 静默同步：不弹窗，只记日志
                "auto_scan_on_start": True,
            },
            "safety": {
                "keep_local_copy": True,           # 覆盖/回滚前自动留底
                "keep_snapshots": 30,              # 每款游戏最多保留多少快照
                "confirm_dangerous": True,         # 危险操作二次确认
            },
            "window": {"width": 1100, "height": 720},
        }
        self.load()

    # ---------- 读写 ----------
    def load(self):
        if os.path.isfile(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    loaded = json.load(f)
                self._merge(self.data, loaded)
            except Exception:
                pass

    @staticmethod
    def _merge(dst, src):
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                Config._merge(dst[k], v)
            else:
                dst[k] = v

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    # ---------- 便捷访问 ----------
    @property
    def vault_root(self) -> str:
        return self.data["vault_root"]

    @property
    def remote_conf(self) -> dict:
        return self.data["remote"]

    @property
    def cloud_enabled(self) -> bool:
        """云端同步是否启用。

        两层条件：总开关打开，且对应后端确实填了地址/目录。
        只勾了开关却没填 WebDAV 地址，等同于没配 —— 按本地模式跑，
        否则每次同步都会去连一个空地址然后报错。
        """
        r = self.data["remote"]
        if not r.get("enabled"):
            return False
        if r.get("type") == "webdav":
            return bool((r.get("url") or "").strip())
        return bool((r.get("path") or "").strip())

    def cloud_label(self) -> str:
        """给界面显示的一行云端描述。未启用时说明当前是本地模式。"""
        if not self.cloud_enabled:
            return "本地备份模式（未配置云端）"
        r = self.data["remote"]
        if r.get("type") == "webdav":
            return "云端已配置：WebDAV %s" % r.get("url", "")
        return "云端已配置：%s" % r.get("path", "")

    @property
    def schedule(self) -> dict:
        return self.data["schedule"]

    @property
    def safety(self) -> dict:
        return self.data["safety"]

    def set(self, dotted: str, value):
        keys = dotted.split(".")
        node = self.data
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = value
        self.save()
