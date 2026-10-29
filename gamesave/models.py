# -*- coding: utf-8 -*-
"""
数据模型。全部用 dataclass，便于测试与序列化。
"""
from dataclasses import dataclass, field, asdict
from typing import List, Optional


# ---------- 游戏 ----------
@dataclass
class Game:
    id: str                      # 稳定标识（路径的短哈希）
    name: str                    # 扫描/添加时的名字，是游戏的稳定标识，不随匹配改变
    paths: List[str]             # 存档目录，可多个
    platform: str = ""           # Steam / Epic / 独立 ...（仅作展示）
    auto_scan: bool = True       # 是否参与定时自动备份
    created_at: float = 0.0
    note: str = ""

    # ---- 元数据（来自 game_meta 表，由 gamemeta 匹配后回填）----
    # 这三个字段不参与 add_game/save_game：它们存在独立的表里，
    # 匹配失败、清空缓存都不该影响「游戏本身」这条记录。
    name_zh: str = ""            # 匹配到的官方中文名，空则界面显示 name
    logo_file: str = ""          # 图标文件名（相对图标目录），不是绝对路径
    meta_state: str = ""         # ok / not_found / error / 空（从未匹配）

    @property
    def display_name(self) -> str:
        """界面显示用：优先中文名，没有就退回原名。

        匹配失败（name_zh 为空）时界面完全不受影响，这是需求里
        「保留游戏原有信息」的落点。
        """
        return self.name_zh or self.name


# ---------- 单个文件的快照相册 ----------
@dataclass
class ChunkRef:
    """一个 chunk 的引用：内容哈希 + 原始体积。

    体积必须随引用一起存：同步前要估算「还得传多少字节」做进度条，
    只靠哈希是算不出大小的。
    """
    sha256: str
    size: int


@dataclass
class FileEntry:
    """快照里的一个文件。

    path    相对存档根目录的路径（统一用 / 分隔，跨平台）
    sha256  整文件内容哈希，用于判断本地/云端是否一致
    size    字节数
    mtime  本地修改时间（仅展示用。Cemu 之类虚拟时钟会让它失去意义，
            因此同步判定一律以内容哈希为准，不采信 mtime）
    chunks  chunk 引用序列，按顺序拼接即还原文件
    """
    path: str
    sha256: str
    size: int
    mtime: float
    chunks: List[ChunkRef] = field(default_factory=list)


@dataclass
class Snapshot:
    """一次备份的完整快照。"""
    id: str
    game_id: str
    created_at: float
    kind: str                    # manual / auto / restore
    files: List[FileEntry] = field(default_factory=list)
    total_size: int = 0
    note: str = ""

    def to_dict(self):
        d = asdict(self)
        return d

    @staticmethod
    def from_dict(d):
        files = []
        for f in d.get("files", []):
            f = dict(f)
            f["chunks"] = [ChunkRef(**c) for c in f.get("chunks", [])]
            files.append(FileEntry(**f))
        return Snapshot(id=d["id"], game_id=d["game_id"], created_at=d["created_at"],
                        kind=d.get("kind", "manual"), files=files,
                        total_size=d.get("total_size", 0), note=d.get("note", ""))


# ---------- 同步状态 ----------
# 云端关闭（本地备份模式）下复用同一套状态，只是含义换成「本地 vs 最新快照」：
#     synced   已备份      本地与最新快照一致
#     local    待备份      本地有新改动，还没进快照
#     unknown  未备份      一款快照都还没有
# 界面的文字由 theme.state_label(state, cloud_on) 按模式切换，
# 状态机本身不需要为本地模式单开一套常量。
SYNC_SYNCED = "synced"          # 本地 = 云端
SYNC_LOCAL_AHEAD = "local"      # 本地有新内容，待上传
SYNC_REMOTE_AHEAD = "remote"    # 云端有新内容，待下载
SYNC_CONFLICT = "conflict"      # 两边都改了且不一致
SYNC_UNKNOWN = "unknown"        # 还没同步过
SYNC_ERROR = "error"            # 上次同步失败


@dataclass
class GameStatus:
    game_id: str
    state: str = SYNC_UNKNOWN
    last_sync_at: float = 0.0
    local_size: int = 0          # 当前本地存档占用
    remote_size: int = 0         # 云端最新快照占用
    snapshot_count: int = 0
    pending_upload: int = 0      # 待上传字节
    pending_download: int = 0    # 待下载字节
    message: str = ""


# ---------- 冲突 ----------
RESOLVE_KEEP_LOCAL = "keep_local"
RESOLVE_USE_REMOTE = "use_remote"
RESOLVE_SAVE_BOTH = "save_both"


@dataclass
class Conflict:
    game_id: str
    game_name: str
    path: str                   # 冲突文件的相对路径
    local_sha: str
    remote_sha: str
    local_size: int
    remote_size: int
    local_mtime: float
    remote_mtime: float
