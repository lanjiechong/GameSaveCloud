# -*- coding: utf-8 -*-
"""
内容寻址的 chunk store —— 去重的实体。

目录结构：
    chunks/
      ab/
        cdef0123...        <- 首字节算法标签 + 压缩载荷，文件名即原始内容 SHA-256
      _tmp/                <- 正在写入的 .part 临时文件，写完原子改名

同一个 chunk 在磁盘上只有一份。游戏 A 的存档和游戏 B 的存档只要内容相同就共享；
同一游戏的相邻两个版本通常只差几个 chunk，所以「保留 10 个历史版本」的
额外磁盘开销远小于 10 倍 —— 这正是去重存储相对「每次整包复制」的核心优势。
"""
import os
import threading

from . import chunker


class ChunkStore:
    def __init__(self, root: str):
        self.root = root
        os.makedirs(self.root, exist_ok=True)
        os.makedirs(self._tmp_dir(), exist_ok=True)

    # ---------- 路径 ----------
    def _tmp_dir(self) -> str:
        return os.path.join(self.root, "_tmp")

    def _tmp_path(self, sha: str) -> str:
        """临时文件名带线程号。

        多个工作线程可能同时拿到同一个 chunk（比如两个游戏同步队列交叠，
        或刷新状态与备份并发）。若共用 <sha>.part，两个写流会交错写同一个
        文件，且一方 os.replace 时另一方还持有打开句柄 —— Windows 下
        replace 会直接以 PermissionError 失败。加上线程号各自为政，
        而同一 sha 的内容必然相同，谁先 replace 成功都无所谓。
        """
        return os.path.join(self._tmp_dir(), "%s.%d.part" % (sha, threading.get_ident()))

    def path_of(self, sha: str) -> str:
        return os.path.join(self.root, sha[:2], sha)

    # ---------- 查询 ----------
    def has(self, sha: str) -> bool:
        return os.path.isfile(self.path_of(sha))

    def missing(self, shas) -> list:
        """返回本地缺失的 chunk 列表（同步时用来算「需要传多少」）。"""
        return [s for s in shas if not self.has(s)]

    def total_size(self) -> int:
        n = 0
        for r, _, fs in os.walk(self.root):
            if os.path.basename(r) == "_tmp":
                continue
            for f in fs:
                try:
                    n += os.path.getsize(os.path.join(r, f))
                except OSError:
                    pass
        return n

    def count(self) -> int:
        n = 0
        for r, _, fs in os.walk(self.root):
            if os.path.basename(r) == "_tmp":
                continue
            n += len(fs)
        return n

    # ---------- 写入 ----------
    def put(self, sha: str, blob: bytes) -> bool:
        """写入一个 chunk（blob = 标签 + 载荷）。已存在则跳过，返回是否真正写入。"""
        dst = self.path_of(sha)
        if os.path.isfile(dst):
            return False
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = self._tmp_path(sha)
        with open(tmp, "wb") as f:
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dst)      # 原子替换，避免半截文件被当成有效 chunk
        return True

    def put_from_file(self, sha: str, src_path: str) -> bool:
        if os.path.isfile(self.path_of(sha)):
            return False
        os.makedirs(os.path.dirname(self.path_of(sha)), exist_ok=True)
        tmp = self._tmp_path(sha)
        with open(src_path, "rb") as fi, open(tmp, "wb") as fo:
            while True:
                b = fi.read(1024 * 256)
                if not b:
                    break
                fo.write(b)
        os.replace(tmp, self.path_of(sha))
        return True

    # ---------- 读取 ----------
    def read(self, sha: str) -> bytes:
        with open(self.path_of(sha), "rb") as f:
            return f.read()

    def read_decompressed(self, sha: str) -> bytes:
        """按 chunk 自描述的首字节标签解压。"""
        blob = self.read(sha)
        return chunker.decompress(blob[:1], blob[1:])

    def iter_payload(self, sha: str, size: int = 1024 * 256):
        """跳过首字节标签，按块迭代压缩载荷（大 chunk 下载时用）。"""
        with open(self.path_of(sha), "rb") as f:
            f.seek(1)
            while True:
                b = f.read(size)
                if not b:
                    break
                yield b

    def tag_of(self, sha: str) -> bytes:
        with open(self.path_of(sha), "rb") as f:
            return f.read(1)

    # ---------- 校验 ----------
    def verify(self, sha: str) -> bool:
        """解压后重新哈希，确认 chunk 没损坏。"""
        try:
            return chunker.sha256_hex(self.read_decompressed(sha)) == sha
        except Exception:
            return False

    def gc(self, referenced_shas) -> int:
        """清理没有被任何快照引用的 chunk。返回删除的 chunk 数。"""
        keep = set(referenced_shas)
        removed = 0
        for r, _, fs in os.walk(self.root):
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
