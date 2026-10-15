# -*- coding: utf-8 -*-
"""
同步引擎：三方合并（本地 / 基准 / 云端）。

为什么需要「基准」而不是简单的两方对比：
    只有本地和云端两个点时，「哪边改的」无法判断 —— 两边不一样就只能是冲突。
    引入 base（上次成功同步时的快照）后，任意一个文件都能归因：
        本地变了、云端没变  → 上传
        云端变了、本地没变  → 下载
        两边都变了且不一致  → 冲突，交给人决定
        两边一样            → 无事发生
这正是 Git / Syncthing / Dropbox 共同的做法。

所有判断都基于**内容哈希**，不使用修改时间。
存档的时间戳不可信（Cemu 用虚拟时钟，时间戳永远是 2077-07-07），
按 mtime 判断新旧必然丢数据。
"""
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import chunker
from .vault import safe_join
from .models import (Game, Snapshot, FileEntry, GameStatus, Conflict,
                     SYNC_SYNCED, SYNC_LOCAL_AHEAD, SYNC_REMOTE_AHEAD,
                     SYNC_CONFLICT, SYNC_UNKNOWN, SYNC_ERROR,
                     RESOLVE_KEEP_LOCAL, RESOLVE_USE_REMOTE, RESOLVE_SAVE_BOTH)

ACT_UPLOAD = "upload"          # 本地 → 云端
ACT_DOWNLOAD = "download"      # 云端 → 本地
ACT_NONE = "none"
ACT_DEL_LOCAL = "del_local"    # 云端删了，本地也删
ACT_DEL_REMOTE = "del_remote"  # 本地删了，云端也删
ACT_CONFLICT = "conflict"


@dataclass
class FileAction:
    path: str
    action: str
    reason: str = ""
    local: Optional[FileEntry] = None
    remote: Optional[FileEntry] = None
    resolution: str = ""                    # 冲突已被用户裁决后填入
    conflict_copy_path: str = ""            # save_both 时云端版本的落点


@dataclass
class SyncPlan:
    game_id: str
    game_name: str
    actions: List[FileAction] = field(default_factory=list)
    conflicts: List[Conflict] = field(default_factory=list)
    base: Optional[Snapshot] = None
    remote: Optional[Snapshot] = None
    local_entries: Dict[str, FileEntry] = field(default_factory=dict)
    upload_bytes: int = 0
    download_bytes: int = 0
    error: str = ""

    @property
    def has_conflict(self) -> bool:
        return bool(self.conflicts)

    def summary(self) -> str:
        up = sum(1 for a in self.actions if a.action == ACT_UPLOAD)
        down = sum(1 for a in self.actions if a.action == ACT_DOWNLOAD)
        cf = len(self.conflicts)
        return "上传 %d · 下载 %d · 冲突 %d" % (up, down, cf)


class SyncEngine:
    def __init__(self, vault, remote, log=None, cloud_on=True):
        self.vault = vault
        self.remote = remote
        self.log = log or (lambda *a: None)
        # cloud_on=False 时进入「本地备份模式」：
        # 不看云端快照、不算上传下载、不调任何远端原语。
        # 状态改由「本地目录 vs 本地最新快照」得出。
        self.cloud_on = bool(cloud_on)

    # ==========================================================
    # 状态查询
    # ==========================================================
    def status(self, game: Game) -> GameStatus:
        st = GameStatus(game_id=game.id)
        try:
            # 状态展示走快速路径：复用最新快照的 FileEntry，未变化的文件
            # 不读盘、不重算哈希。真正的 plan()/execute() 仍然全量精算。
            latest = self.vault.latest_snapshot(game.id)
            hint = {e.path: e for e in latest.files} if latest else None
            local = self.vault.scan_local(game, quick_hint=hint)
            st.local_size = sum(e.size for e in local)
        except Exception as e:
            st.state = SYNC_ERROR
            st.message = "扫描本地失败: %s" % e
            return st

        # ---- 本地备份模式：只对比本地目录与本地最新快照 ----
        if not self.cloud_on:
            return self._status_local(game, st, local)

        try:
            remote_snaps = self.remote.list_snapshots(game.id)
        except Exception as e:
            st.state = SYNC_ERROR
            st.message = "连接云端失败: %s" % e
            return st
        st.snapshot_count = len(remote_snaps)
        if remote_snaps:
            st.remote_size = remote_snaps[0].total_size
        ss = self.vault.get_sync_state(game.id)
        if ss and ss.get("last_sync_at"):
            st.last_sync_at = ss["last_sync_at"]
        if not remote_snaps and not (ss and ss.get("base_snapshot_id")):
            st.state = SYNC_UNKNOWN
            st.message = "尚未同步过"
            return st
        plan = self.plan(game)
        st.pending_upload = plan.upload_bytes
        st.pending_download = plan.download_bytes
        if plan.error:
            st.state = SYNC_ERROR
            st.message = plan.error
        elif plan.has_conflict:
            st.state = SYNC_CONFLICT
            st.message = "%d 个文件本地与云端不一致" % len(plan.conflicts)
        elif plan.upload_bytes or any(a.action == ACT_UPLOAD for a in plan.actions):
            st.state = SYNC_LOCAL_AHEAD
            st.message = "本地有新进度待上传"
        elif plan.download_bytes or any(a.action == ACT_DOWNLOAD for a in plan.actions):
            st.state = SYNC_REMOTE_AHEAD
            st.message = "云端有新进度待下载"
        else:
            st.state = SYNC_SYNCED
            st.message = "已同步"
        return st

    def _status_local(self, game: Game, st: GameStatus, local) -> GameStatus:
        """本地备份模式的状态：本地目录 vs 本地最新快照。

        语义与云端模式一一对应，只是「远端」换成了「保险库里的最新快照」：
            没有快照                     → 未备份
            本地与最新快照逐文件一致       → 已备份
            有文件较最新快照有增减或改动   → 待备份
        """
        snaps = self.vault.list_snapshots(game.id)
        st.snapshot_count = len(snaps)
        if not snaps:
            st.state = SYNC_UNKNOWN
            st.message = "尚未备份过"
            return st
        latest = snaps[0]
        st.remote_size = latest.total_size        # 界面上显示为「最新备份」
        ss = self.vault.get_sync_state(game.id) or {}
        st.last_sync_at = ss.get("last_sync_at") or latest.created_at
        lmap = {e.path: e for e in local}
        rmap = {e.path: e for e in latest.files}
        dirty = [p for p in set(lmap) | set(rmap)
                 if (lmap[p].sha256 if p in lmap else None)
                 != (rmap[p].sha256 if p in rmap else None)]
        if dirty:
            st.state = SYNC_LOCAL_AHEAD
            st.message = "本地有 %d 个文件较上次备份有变化" % len(dirty)
        else:
            st.state = SYNC_SYNCED
            st.message = "已备份到本地保险库"
        return st

    # ==========================================================
    # 计划：三方合并，只算不写
    # ==========================================================
    def plan(self, game: Game) -> SyncPlan:
        p = SyncPlan(game_id=game.id, game_name=game.name)
        try:
            local_list = self.vault.scan_local(game)
        except Exception as e:
            p.error = "扫描本地存档失败: %s" % e
            return p
        p.local_entries = {e.path: e for e in local_list}

        p.remote = None
        remote_map = {}
        remote_snaps = []
        if self.cloud_on:
            try:
                remote_snaps = self.remote.list_snapshots(game.id)
            except Exception as e:
                p.error = "读取云端快照失败: %s" % e
                return p
            p.remote = remote_snaps[0] if remote_snaps else None
            remote_map = {e.path: e for e in p.remote.files} if p.remote else {}

        # 基准快照：优先本地记录的，其次用云端倒数第二个（首同步时它就是上一版）
        base_map = {}
        ss = self.vault.get_sync_state(game.id)
        base_sid = (ss or {}).get("base_snapshot_id")
        if base_sid and self.cloud_on:
            try:
                p.base = self.remote.get_snapshot(base_sid)
            except Exception:
                try:
                    p.base = self.vault.get_snapshot(base_sid)
                except Exception:
                    p.base = None
        if p.base is None and len(remote_snaps) >= 2:
            p.base = remote_snaps[1]
        if p.base:
            base_map = {e.path: e for e in p.base.files}

        all_paths = set(p.local_entries) | set(remote_map) | set(base_map)
        for path in sorted(all_paths):
            l = p.local_entries.get(path)
            r = remote_map.get(path)
            b = base_map.get(path)
            l_sha = l.sha256 if l else None
            r_sha = r.sha256 if r else None
            b_sha = b.sha256 if b else None

            act, reason = self._decide(l_sha, r_sha, b_sha)
            fa = FileAction(path=path, action=act, reason=reason, local=l, remote=r)

            if act == ACT_UPLOAD:
                shas = [c.sha256 for c in l.chunks]
                # 本地模式下没有「云端已有哪些」可问，按全量算 ——
                # 这个数字只用于展示「打开云端后需要传多少」。
                if self.cloud_on:
                    missing = set(self.remote.missing_chunks(shas))
                else:
                    missing = set(shas)
                p.upload_bytes += sum(c.size for c in l.chunks if c.sha256 in missing)
            elif act == ACT_DOWNLOAD:
                missing = set(self.vault.store.missing([c.sha256 for c in r.chunks]))
                p.download_bytes += sum(c.size for c in r.chunks if c.sha256 in missing)
            elif act == ACT_CONFLICT:
                p.conflicts.append(Conflict(
                    game_id=game.id, game_name=game.name, path=path,
                    local_sha=l_sha or "", remote_sha=r_sha or "",
                    local_size=l.size if l else 0, remote_size=r.size if r else 0,
                    local_mtime=l.mtime if l else 0, remote_mtime=r.mtime if r else 0))
            p.actions.append(fa)
        return p

    @staticmethod
    def _decide(l_sha, r_sha, b_sha):
        """三方合并判定表。返回 (动作, 原因)。"""
        if l_sha == r_sha:
            return ACT_NONE, "两边一致"
        if l_sha == b_sha:
            # 本地没动
            if r_sha is None:
                return ACT_DEL_REMOTE, "云端已删除，本地未改动 → 本地也删除"
            return ACT_DOWNLOAD, "云端有更新，本地未改动 → 下载"
        if r_sha == b_sha:
            # 云端没动
            if l_sha is None:
                return ACT_DEL_LOCAL, "本地已删除，云端未改动 → 云端也删除"
            return ACT_UPLOAD, "本地有更新，云端未改动 → 上传"
        if b_sha is None:
            # 两边都是新增且内容不同
            return ACT_CONFLICT, "两边各自新增了同名文件且内容不同"
        return ACT_CONFLICT, "本地和云端都改过同一文件"

    # ---------- 体积估算辅助 ----------
    @staticmethod
    def _bytes_of(entry: FileEntry, shas) -> int:
        """按 chunk 引用累加原始体积。chunk 体积随引用存在快照里，无需反查 store。"""
        want = set(shas)
        return sum(c.size for c in entry.chunks if c.sha256 in want)

    # ==========================================================
    # 执行
    # ==========================================================
    def execute(self, game: Game, plan: SyncPlan, resolutions: Dict[str, str],
                progress_cb=None, only_upload=False, only_download=False):
        """按计划执行同步。

        resolutions: {相对路径: RESOLVE_*} 用于裁决冲突。
        返回 {"uploaded":n,"downloaded":n,"conflicts":n,"bytes":b,"errors":[...]}
        """
        res = {"uploaded": 0, "downloaded": 0, "conflicts": 0,
               "bytes": 0, "errors": [], "snapshot": None}

        # 1. 应用冲突裁决
        for c in plan.conflicts:
            choice = resolutions.get(c.path)
            if not choice:
                res["conflicts"] += 1
                res["errors"].append("未裁决的冲突: %s" % c.path)
                continue
            fa = next(a for a in plan.actions if a.path == c.path)
            fa.resolution = choice
            if choice == RESOLVE_KEEP_LOCAL:
                fa.action = ACT_UPLOAD
                fa.reason = "冲突裁决：保留本地"
            elif choice == RESOLVE_USE_REMOTE:
                fa.action = ACT_DOWNLOAD
                fa.reason = "冲突裁决：使用云端"
            elif choice == RESOLVE_SAVE_BOTH:
                fa.action = ACT_UPLOAD
                fa.reason = "冲突裁决：本地保留为主，云端另存"
                ts = time.strftime("%Y%m%d_%H%M%S")
                base, ext = os.path.splitext(c.path)
                fa.conflict_copy_path = "%s.云端版本_%s%s" % (base, ts, ext)
            else:
                res["errors"].append("未知裁决 %s: %s" % (choice, c.path))

        # 2. 上传缺失 chunk（断点续传，已存在的直接跳过）
        if not only_download:
            for a in plan.actions:
                if a.action != ACT_UPLOAD or not a.local:
                    continue
                missing = self.remote.missing_chunks([c.sha256 for c in a.local.chunks])
                for sha in missing:
                    try:
                        blob = self.vault.store.read(sha)
                        self.remote.upload_chunk(sha, blob)
                        res["uploaded"] += 1
                        res["bytes"] += len(blob)
                    except Exception as e:
                        res["errors"].append("上传 %s 失败: %s" % (a.path, e))
                if progress_cb:
                    progress_cb("upload", a.path)

        # 3. 下载缺失 chunk
        if not only_upload:
            for a in plan.actions:
                if not a.remote:
                    continue
                # 两类动作需要把云端的 chunk 取回本地：
                #   ACT_DOWNLOAD —— 云端版本要覆盖本地
                #   conflict_copy_path —— save_both 裁决要把云端版本另存一份，
                #     它的 chunk 同样不在本地 store 里，不取回来会 FileNotFound
                if a.action == ACT_DOWNLOAD or a.conflict_copy_path:
                    for sha in self.vault.store.missing(
                            [c.sha256 for c in a.remote.chunks]):
                        dst = self.vault.store.path_of(sha)
                        try:
                            self.remote.download_chunk(sha, dst)
                        except Exception as e:
                            res["errors"].append("下载 %s 失败: %s" % (a.path, e))
                            continue
                        # 下载落盘后立刻解压重算哈希。弱网 / 代理服务器可能
                        # 对 Range 请求返回 200 全量，ab 追加会拼出坏块；
                        # 在这里拦下，坏 chunk 当场删除，绝不进入快照。
                        if not self.vault.store.verify(sha):
                            try:
                                os.remove(dst)
                            except OSError:
                                pass
                            res["errors"].append(
                                "下载的 chunk 校验失败（疑似传输损坏）: %s" % sha)
                        else:
                            res["downloaded"] += 1
                    if progress_cb:
                        progress_cb("download", a.path)

        if res["errors"]:
            # 有失败项就不推进基准，下次重试而不是把半成品当成已同步
            return res

        # 4. 构建合并后的快照
        # 什么都没变就不要造新快照。定时同步每小时跑一次，如果无变化时
        # 也生成一份，一年就是 8760 份几乎相同的清单：既拖慢
        # list_snapshots（它要逐个 json.load），也让「保留 N 个历史版本」
        # 失去意义 —— 30 个配额会被同一份内容占满。
        if not any(a.action != ACT_NONE for a in plan.actions):
            self.vault.set_sync_state(game.id, last_sync_at=time.time(),
                                      last_error="")
            res["snapshot"] = None
            return res

        merged = []
        for a in plan.actions:
            if a.action == ACT_UPLOAD and a.local:
                merged.append(a.local)
            elif a.action == ACT_DOWNLOAD and a.remote:
                merged.append(a.remote)
            elif a.action == ACT_NONE:
                if a.local:
                    merged.append(a.local)
                elif a.remote:
                    merged.append(a.remote)
            # ACT_DEL_* 的文件不进新快照

        snap = Snapshot(id="snap_%s_%d" % (game.id[:8], int(time.time() * 1000)),
                        game_id=game.id, created_at=time.time(), kind="sync",
                        files=merged, total_size=sum(e.size for e in merged),
                        note=plan.summary())
        self.vault._write_snapshot(snap)
        try:
            self.remote.put_snapshot(snap)
        except Exception as e:
            res["errors"].append("上传快照清单失败: %s" % e)
            return res
        res["snapshot"] = snap

        # 5. 把「下载」和「另存的云端版本」写到游戏目录（覆盖前留底）
        for a in plan.actions:
            if a.action == ACT_DOWNLOAD and a.remote:
                self._materialize(game, a.remote, a.path)
            elif a.conflict_copy_path and a.remote:
                self._materialize(game, a.remote, a.conflict_copy_path,
                                  note="云端冲突版本")

        # 6. 本地删除（云端已删的文件）—— 同样先留底
        for a in plan.actions:
            if a.action == ACT_DEL_REMOTE:
                self._delete_local(game, a.path)

        self.vault.set_sync_state(game.id, base_snapshot_id=snap.id,
                                  last_sync_at=time.time(), last_error="")
        return res

    # ---------- 落盘辅助 ----------
    def _resolve(self, game: Game, rel_path: str):
        """快照路径 → 本地真实路径。陈旧序号返回 None（跳过）；
        越界路径抛 UnsafePathError（中止本次同步，不推进 base）。"""
        roots = game.paths
        if len(roots) == 1:
            return safe_join(roots[0], rel_path)
        parts = rel_path.split("/", 1)
        if len(parts) != 2 or not parts[0].isdigit():
            return None
        idx = int(parts[0])
        if idx >= len(roots):
            return None
        return safe_join(roots[idx], parts[1])

    def _materialize(self, game: Game, entry: FileEntry, rel_path: str, note=""):
        dst = self._resolve(game, rel_path)
        if dst is None:
            return
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.isfile(dst):
            self.vault._backup_existing(game.id, rel_path, dst,
                                        reason="sync-overwrite" + (":" + note if note else ""))
        with open(dst, "wb") as out:
            for c in entry.chunks:
                out.write(self.vault.store.read_decompressed(c.sha256))

    def _delete_local(self, game: Game, rel_path: str):
        dst = self._resolve(game, rel_path)
        if dst is None:
            return
        if os.path.isfile(dst):
            self.vault._backup_existing(game.id, rel_path, dst, reason="sync-delete")
            os.remove(dst)
