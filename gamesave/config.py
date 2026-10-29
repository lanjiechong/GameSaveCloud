# -*- coding: utf-8 -*-
"""
应用配置。存 vault/config.json，跨平台。

默认值按平台选合理的家目录，用户可在设置里改。
"""
import json
import os
import sys

from . import secret

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
                # 密码只存 DPAPI 密文（见 gamesave/secret.py），
                # 读写请走 get_remote_password() / set_remote_password()，
                # 不要直接碰这两个键。password 仅为兼容旧配置的遗留明文。
                "password_enc": "",
                "password": "",
            },
            "schedule": {
                "enabled": False,
                "interval_minutes": 60,
                "silent": True,                    # 静默同步：不弹窗，只记日志
                "auto_scan_on_start": True,
                "autostart": False,                # 开机自启（写 HKCU Run 项）
            },
            "safety": {
                "keep_local_copy": True,           # 覆盖/回滚前自动留底
                "keep_snapshots": 30,              # 每款游戏最多保留多少快照
                "confirm_dangerous": True,         # 危险操作二次确认
            },
            "metadata": {
                # 中文名补充源。主源永远是 Steam；Bangumi 只在 Steam 查到
                # 游戏但没给中文名时补一手，前提是能访问 api.bgm.tv
                # （本机 2026-10-30 实测不通，所以默认关 —— 打不通的源常开
                # 只会给每次刷新白加一个超时）。
                # 本地离线索引（ui/assets/gameindex.json）始终启用：
                # 它不产生网络请求，没有关掉的必要。
                "use_bangumi": False,
            },
            "window": {"width": 1100, "height": 720},
        }
        self.load()
        self._migrate_plaintext_password()

    def _migrate_plaintext_password(self):
        """把旧版 config.json 里的明文 WebDAV 密码就地转成 DPAPI 密文。

        只迁移一次；转换后把明文键清空并落盘。DPAPI 不可用（非 Windows）
        时退化为 base64，同样不再保留裸明文。
        """
        r = self.data["remote"]
        if r.get("password") and not r.get("password_enc"):
            try:
                r["password_enc"] = secret.encrypt(r["password"])
                r["password"] = ""
                self.save()
            except Exception:
                # 加密失败就保留原状，至少不影响应用启动
                pass

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

    def get_remote_password(self) -> str:
        """取 WebDAV 明文密码（内存中解密），不落日志、不进 UI 文案。"""
        r = self.data["remote"]
        if r.get("password_enc"):
            try:
                return secret.decrypt(r["password_enc"])
            except Exception:
                # 例如密文是在另一台机器/另一个用户下生成的
                return ""
        return r.get("password", "") or ""

    def set_remote_password(self, plaintext: str):
        """设置密码：加密后存 password_enc，明文键始终清空。"""
        r = self.data["remote"]
        r["password_enc"] = secret.encrypt(plaintext or "")
        r["password"] = ""

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
