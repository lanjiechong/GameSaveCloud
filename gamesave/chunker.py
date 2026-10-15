# -*- coding: utf-8 -*-
"""
分块、哈希与压缩。

设计要点：
- 固定 1 MiB 分块。对存档这类「整体不大但会反复写」的文件，
  固定分块已足够：改动一个区块只会让那一个 chunk 失效，
  其余 chunk 的哈希不变，天然支持增量同步与跨游戏去重。
- 每个 chunk 用 SHA-256 做内容寻址，压缩后按哈希存入 chunk store。
  同一份数据（哪怕来自不同游戏、不同版本）只存一份。
- 压缩优先用 zstandard（若有），否则退回标准库 zlib，
  保证无第三方依赖时也能完整运行。
"""
import hashlib
import os
import zlib

CHUNK_SIZE = 1024 * 1024          # 1 MiB
ZSTD_LEVEL = 3                    # 存档体积小，低压缩等级换取速度

try:                              # 可选加速，缺失时自动降级
    import zstandard as _zstd
    _HAS_ZSTD = True
except Exception:                 # pragma: no cover - 环境相关
    _zstd = None
    _HAS_ZSTD = False

# 压缩算法标识，作为 chunk 文件的首字节存放，使 chunk 自描述：
# 任何后端（本地目录 / WebDAV / 对象存储）拿到文件就能解压，不依赖外部元数据。
TAG_ZSTD = b"z"
TAG_ZLIB = b"l"
TAG_RAW = b"r"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compress(data: bytes) -> tuple:
    """返回 (标签字节, 载荷)。压缩后更大则原样存放。"""
    if _HAS_ZSTD:
        try:
            out = _zstd.ZstdCompressor(level=ZSTD_LEVEL).compress(data)
            if len(out) < len(data):
                return TAG_ZSTD, out
        except Exception:
            pass
    out = zlib.compress(data, 6)
    if len(out) < len(data):
        return TAG_ZLIB, out
    return TAG_RAW, data


def decompress(tag: bytes, payload: bytes) -> bytes:
    if tag == TAG_ZSTD and _HAS_ZSTD:
        return _zstd.ZstdDecompressor().decompress(payload)
    if tag == TAG_ZLIB:
        return zlib.decompress(payload)
    if tag == TAG_ZSTD and not _HAS_ZSTD:
        raise RuntimeError("该 chunk 用 zstd 压缩，但当前环境未安装 zstandard")
    return payload


def chunk_file(path: str, progress_cb=None) -> tuple:
    """把文件切成 chunk 列表，同时增量算出整文件哈希。

    返回 (chunks, file_sha256)，其中 chunks 为
    [{"sha256":..., "size":原始字节数, "blob":标签+压缩载荷}, ...]
    sha256 是**原始内容**的哈希，因此与压缩算法无关，可跨算法复用同一份数据。
    整文件哈希在分块过程中增量计算，避免为算校验值再把文件读一遍。
    """
    chunks = []
    h = hashlib.sha256()
    total = os.path.getsize(path)
    done = 0
    with open(path, "rb") as f:
        while True:
            raw = f.read(CHUNK_SIZE)
            if not raw:
                break
            h.update(raw)
            tag, payload = compress(raw)
            chunks.append({
                "sha256": sha256_hex(raw),
                "size": len(raw),
                "blob": tag + payload,
            })
            done += len(raw)
            if progress_cb:
                progress_cb(done, total)
    return chunks, h.hexdigest()


def hash_file(path: str) -> str:
    """整文件 SHA-256，用于快速判断本地/云端是否一致。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(CHUNK_SIZE)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0
    return "%.1f TB" % n
