# -*- coding: utf-8 -*-
"""
云端后端抽象。

RemoteBackend 只暴露 5 类原语：
    快照清单 / 读写快照 / chunk 存在性 / 上传 chunk / 下载 chunk
上层 SyncEngine 不关心背后是本地目录、WebDAV 还是对象存储。

内置两个实现：
    LocalFolderBackend  一个目录（可以是另一块盘、NAS 挂载、或 OneDrive/Dropbox
                        同步目录）。零依赖、开箱即用，也是测试与演示的默认后端。
    WebDAVBackend       标准 WebDAV（坚果云、Nextcloud、群晖等均支持），
                        只用标准库 urllib，无需第三方包。
"""
import json
import os
import shutil
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod

from .models import Snapshot


class RemoteError(Exception):
    pass


class RemoteBackend(ABC):
    name = "abstract"

    @abstractmethod
    def test(self):
        """返回 (ok, message)"""

    @abstractmethod
    def list_snapshots(self, game_id: str) -> list:
        """返回该游戏在云端的所有快照，按时间倒序。"""

    @abstractmethod
    def get_snapshot(self, sid: str) -> Snapshot:
        """按 id 取云端快照。"""

    @abstractmethod
    def put_snapshot(self, snap: Snapshot):
        """上传快照清单。"""

    @abstractmethod
    def delete_snapshot(self, sid: str):
        pass

    @abstractmethod
    def missing_chunks(self, shas) -> list:
        """返回云端缺失的 chunk 列表。"""

    @abstractmethod
    def upload_chunk(self, sha: str, blob: bytes, progress_cb=None):
        """上传单个 chunk。"""

    @abstractmethod
    def download_chunk(self, sha: str, dst_path: str, progress_cb=None):
        """下载单个 chunk 到本地文件。"""

    def prune_snapshots(self, game_id: str, keep: int, protect=None) -> int:
        """裁剪云端快照。默认不支持，返回 0。"""
        return 0


# ==================================================================
# 本地目录后端
# ==================================================================
class LocalFolderBackend(RemoteBackend):
    """把一个目录当作「云端」。

    适合：另一块硬盘 / 移动硬盘 / NAS 挂载盘 / OneDrive·Dropbox 同步目录。
    目录布局与本地 vault 一致，因此可以直接把整块盘拔下来当备份用。
    """
    name = "本地目录"

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.chunks_dir = os.path.join(self.root, "chunks")
        self.snapshots_dir = os.path.join(self.root, "snapshots")
        os.makedirs(self.chunks_dir, exist_ok=True)
        os.makedirs(self.snapshots_dir, exist_ok=True)

    # ---------- 快照 ----------
    def _snap_path(self, sid: str) -> str:
        return os.path.join(self.snapshots_dir, sid + ".json")

    def test(self):
        try:
            probe = os.path.join(self.root, ".probe")
            with open(probe, "w") as f:
                f.write("ok")
            os.remove(probe)
            return True, "目录可写：%s" % self.root
        except Exception as e:
            return False, "目录不可写：%s" % e

    def list_snapshots(self, game_id: str) -> list:
        out = []
        for f in os.listdir(self.snapshots_dir):
            if not f.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.snapshots_dir, f), encoding="utf-8") as fh:
                    d = json.load(fh)
                if d.get("game_id") != game_id:
                    continue
                out.append(Snapshot.from_dict(d))
            except Exception:
                continue
        out.sort(key=lambda s: s.created_at, reverse=True)
        return out

    def get_snapshot(self, sid: str) -> Snapshot:
        p = self._snap_path(sid)
        if not os.path.isfile(p):
            raise RemoteError("云端不存在快照 %s" % sid)
        with open(p, encoding="utf-8") as f:
            return Snapshot.from_dict(json.load(f))

    def put_snapshot(self, snap: Snapshot):
        with open(self._snap_path(snap.id), "w", encoding="utf-8") as f:
            json.dump(snap.to_dict(), f, ensure_ascii=False, indent=1)

    def delete_snapshot(self, sid: str):
        p = self._snap_path(sid)
        if os.path.isfile(p):
            os.remove(p)

    # ---------- chunk ----------
    def _chunk_path(self, sha: str) -> str:
        return os.path.join(self.chunks_dir, sha[:2], sha)

    def missing_chunks(self, shas) -> list:
        return [s for s in shas if not os.path.isfile(self._chunk_path(s))]

    def upload_chunk(self, sha: str, blob: bytes, progress_cb=None):
        dst = self._chunk_path(sha)
        if os.path.isfile(dst):
            if progress_cb:
                progress_cb(len(blob), len(blob))
            return False
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = os.path.join(self.chunks_dir, "_tmp")
        os.makedirs(tmp, exist_ok=True)
        part = os.path.join(tmp, sha + ".part")
        # 断点续传：已有 .part 且大小一致就认为传完了，直接改名
        if os.path.isfile(part) and os.path.getsize(part) == len(blob):
            os.replace(part, dst)
            return True
        with open(part, "wb") as f:
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        os.replace(part, dst)
        if progress_cb:
            progress_cb(len(blob), len(blob))
        return True

    def download_chunk(self, sha: str, dst_path: str, progress_cb=None):
        src = self._chunk_path(sha)
        if not os.path.isfile(src):
            raise RemoteError("云端缺少 chunk %s" % sha)
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        tmp = dst_path + ".part"
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst_path)
        if progress_cb:
            progress_cb(os.path.getsize(dst_path), os.path.getsize(dst_path))

    def total_size(self) -> int:
        n = 0
        for r, _, fs in os.walk(self.chunks_dir):
            if os.path.basename(r) == "_tmp":
                continue
            for f in fs:
                try:
                    n += os.path.getsize(os.path.join(r, f))
                except OSError:
                    pass
        return n

    # ---------- 维护 ----------
    def all_referenced_chunks(self) -> set:
        """扫全部云端快照，收集仍被引用的 chunk 哈希。"""
        keep = set()
        for f in os.listdir(self.snapshots_dir):
            if not f.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.snapshots_dir, f), encoding="utf-8") as fh:
                    d = json.load(fh)
                for fe in d.get("files", []):
                    for c in fe.get("chunks", []):
                        keep.add(c["sha256"])
            except Exception:
                continue
        return keep

    def gc(self) -> int:
        """清理云端没有任何快照引用的 chunk。返回删除数。"""
        keep = self.all_referenced_chunks()
        removed = 0
        for r, _, fs in os.walk(self.chunks_dir):
            if os.path.basename(r) == "_tmp":
                continue
            for f in fs:
                if f not in keep:
                    try:
                        os.remove(os.path.join(r, f))
                        removed += 1
                    except OSError:
                        pass
        return removed

    def prune_snapshots(self, game_id: str, keep: int, protect=None) -> int:
        """只保留该游戏最近的 keep 份云端快照，并回收失去引用的 chunk。

        不裁剪的话， hourly 的定时同步会让备份目标无限增长 ——
        那正是「去重存储」想要避免的事情。
        protect 里的快照 id 不删（通常是当前同步基准）。
        """
        if not keep or keep <= 0:
            return 0
        protect = protect or set()
        snaps = self.list_snapshots(game_id)          # created_at 倒序
        removed = 0
        for s in snaps[keep:]:
            if s.id in protect:
                continue
            self.delete_snapshot(s.id)
            removed += 1
        if removed:
            self.gc()
        return removed


# ==================================================================
# WebDAV 后端
# ==================================================================
class WebDAVBackend(RemoteBackend):
    """标准 WebDAV。仅用标准库，无需 requests。

    用法：WebDAVBackend("https://dav.jianguoyun.com/dav/GameSaveCloud",
                        user="...", password="...")
    目录结构：<base>/chunks/ab/<sha>、<base>/snapshots/<sid>.json
    """
    name = "WebDAV"

    def __init__(self, base_url: str, user: str = "", password: str = "",
                 timeout: int = 30):
        self.base = base_url.rstrip("/") + "/"
        self.user = user
        self.password = password
        self.timeout = timeout
        self._auth = self._make_auth()

    def _make_auth(self):
        import base64
        raw = ("%s:%s" % (self.user, self.password)).encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")

    def _url(self, *parts) -> str:
        return self.base + "/".join(urllib.parse.quote(str(p), safe="") for p in parts)

    def _req(self, method: str, url: str, data=None, headers=None, stream=False):
        h = {"Authorization": self._auth}
        if headers:
            h.update(headers)
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)
            return resp
        except urllib.error.HTTPError as e:
            if e.code in (404, 409):
                raise RemoteError("HTTP %s %s" % (e.code, url))
            raise RemoteError("HTTP %s %s" % (e.code, url))
        except Exception as e:
            raise RemoteError("%s: %s" % (type(e).__name__, e))

    def _mkcol(self, url: str):
        try:
            self._req("MKCOL", url)
        except RemoteError:
            pass  # 已存在

    def test(self):
        try:
            self._mkcol(self._url("chunks"))
            self._mkcol(self._url("snapshots"))
            probe = self._url(".probe")
            self._req("PUT", probe, data=b"ok")
            try:
                self._req("DELETE", probe)
            except RemoteError:
                pass
            return True, "WebDAV 连接正常：%s" % self.base
        except Exception as e:
            return False, "WebDAV 连接失败：%s" % e

    # ---------- 快照 ----------
    def list_snapshots(self, game_id: str) -> list:
        out = []
        try:
            resp = self._req("PROPFIND", self._url("snapshots"),
                             headers={"Depth": "1"})
            body = resp.read().decode("utf-8", "replace")
        except RemoteError:
            return []
        import re
        for m in re.finditer(r"<d:href>([^<]+)</d:href>", body):
            href = urllib.parse.unquote(m.group(1))
            sid = os.path.basename(href.rstrip("/"))
            if not sid.endswith(".json"):
                continue
            sid = sid[:-5]
            try:
                out.append(self.get_snapshot(sid))
            except Exception:
                continue
        out = [s for s in out if s.game_id == game_id]
        out.sort(key=lambda s: s.created_at, reverse=True)
        return out

    def get_snapshot(self, sid: str) -> Snapshot:
        try:
            resp = self._req("GET", self._url("snapshots", sid + ".json"))
            data = resp.read()
        except RemoteError as e:
            raise RemoteError("读取快照失败 %s: %s" % (sid, e))
        return Snapshot.from_dict(json.loads(data.decode("utf-8")))

    def put_snapshot(self, snap: Snapshot):
        data = json.dumps(snap.to_dict(), ensure_ascii=False, indent=1).encode("utf-8")
        self._req("PUT", self._url("snapshots", snap.id + ".json"), data=data,
                  headers={"Content-Type": "application/json"})

    def delete_snapshot(self, sid: str):
        try:
            self._req("DELETE", self._url("snapshots", sid + ".json"))
        except RemoteError:
            pass

    # ---------- chunk ----------
    def _chunk_url(self, sha: str) -> str:
        return self._url("chunks", sha[:2], sha)

    def _chunk_head_size(self, sha: str):
        try:
            resp = self._req("HEAD", self._chunk_url(sha))
            return int(resp.headers.get("Content-Length", "0"))
        except RemoteError:
            return None

    def missing_chunks(self, shas) -> list:
        return [s for s in shas if self._chunk_head_size(s) is None]

    def upload_chunk(self, sha: str, blob: bytes, progress_cb=None):
        url = self._chunk_url(sha)
        # 断点续传：先问云端已有多少字节，从断点处续传
        have = self._chunk_head_size(sha)
        if have == len(blob):
            if progress_cb:
                progress_cb(len(blob), len(blob))
            return False
        if have and have < len(blob):
            self._req("PATCH" if False else "PUT", url,
                      data=blob[have:], headers={"Content-Range": "bytes %d-*/%d"
                                                 % (have, len(blob))})
        else:
            self._req("PUT", url, data=blob)
        if progress_cb:
            progress_cb(len(blob), len(blob))
        return True

    def download_chunk(self, sha: str, dst_path: str, progress_cb=None):
        url = self._chunk_url(sha)
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        tmp = dst_path + ".part"
        have = os.path.getsize(tmp) if os.path.isfile(tmp) else 0
        headers = {}
        if have:
            headers["Range"] = "bytes=%d-" % have
        try:
            resp = self._req("GET", url, headers=headers)
            mode = "ab" if have else "wb"
            with open(tmp, mode) as f:
                while True:
                    b = resp.read(256 * 1024)
                    if not b:
                        break
                    f.write(b)
                    if progress_cb:
                        progress_cb(have + f.tell(), -1)
            os.replace(tmp, dst_path)
        except RemoteError as e:
            raise RemoteError("下载 chunk %s 失败: %s" % (sha, e))
