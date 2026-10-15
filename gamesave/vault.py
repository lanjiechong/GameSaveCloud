# -*- coding: utf-8 -*-
"""
本地保险库（Vault）。

职责：
- 维护 vault 目录结构与 SQLite 索引
- 把游戏的存档目录「快照」成内容寻址的 chunk 集合
- 还原快照，覆盖前自动留底
- 记录同步基准（base snapshot），供三方合并使用
- 回收没有被任何快照引用的 chunk

目录结构：
    vault/
      chunks/            内容寻址 chunk store（去重 + 压缩）
      snapshots/         每个快照一个 JSON（人可读，便于排查）
      localcopies/       覆盖/回滚前的安全留底
      index.db           SQLite
      config.json        应用配置
"""
import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time

from . import chunker
from .models import Game, Snapshot, FileEntry, ChunkRef
from .store import ChunkStore

SCHEMA = """
CREATE TABLE IF NOT EXISTS games(
  id TEXT PRIMARY KEY, name TEXT, paths TEXT, platform TEXT,
  auto_scan INTEGER, created_at REAL, note TEXT);
CREATE TABLE IF NOT EXISTS snapshots(
  id TEXT PRIMARY KEY, game_id TEXT, created_at REAL, kind TEXT,
  total_size INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS files(
  snapshot_id TEXT, game_id TEXT, path TEXT, sha256 TEXT,
  size INTEGER, mtime REAL, chunks TEXT,
  PRIMARY KEY(snapshot_id, path));
CREATE INDEX IF NOT EXISTS idx_files_game ON files(game_id, path);
CREATE TABLE IF NOT EXISTS chunk_refs(
  sha TEXT, snapshot_id TEXT, PRIMARY KEY(sha, snapshot_id));
CREATE TABLE IF NOT EXISTS sync_state(
  game_id TEXT PRIMARY KEY, base_snapshot_id TEXT,
  last_sync_at REAL, last_error TEXT);
CREATE TABLE IF NOT EXISTS local_copies(
  id INTEGER PRIMARY KEY AUTOINCREMENT, game_id TEXT, path TEXT,
  backup_path TEXT, created_at REAL, reason TEXT);
"""


def new_id(seed: str) -> str:
    return hashlib.sha1(("%s|%f" % (seed, time.time())).encode("utf-8")).hexdigest()[:16]


class UnsafePathError(Exception):
    """快照内相对路径试图逃逸出存档根目录（含 ``..`` 或盘符等）。

    远端快照清单属于不可信输入（WebDAV 服务器可能被攻陷，或走明文
    http 被中间人篡改），不能让其中的路径把文件写到存档目录之外。
    """


def safe_join(root: str, rel: str) -> str:
    """把快照内相对路径（``/`` 分隔）拼到 root 下，并保证结果不逃出 root。

    目标文件允许尚不存在（realpath 只做词法归一化），所以新建目录
    场景一样能校验。越界（含 ``..`` 回溯、盘符绝对路径等）抛
    UnsafePathError。
    """
    base = os.path.realpath(root)
    dst = os.path.realpath(os.path.join(base, rel.replace("/", os.sep)))
    if dst != base and not dst.startswith(base + os.sep):
        raise UnsafePathError("快照路径越出存档根目录: %s" % rel)
    return dst


class _LockedDB:
    """给 sqlite3.Connection 套一把锁，让它能被多个线程共用。

    背景：本应用所有磁盘/网络 IO 都通过 QThreadPool 丢到工作线程，
    而 Vault（连同它里面的 SQLite 连接）是主线程构造出来的单例。
    sqlite3 默认把连接绑定到创建它的线程，于是第一个后台任务就炸：

        ProgrammingError: SQLite objects created in a thread can only be
        used in that same thread. The object was created in thread id
        43400 and this is thread id 48072.

    打开 check_same_thread=False 只是解除模块层的检查；SQLite 连接内部
    还有语句缓存、事务状态、行游标等非线程安全的东西，所以还必须用一把
    可重入锁把所有访问串行化。本应用写操作都发生在工作线程、读操作穿插
    其中，锁的持有时间都是毫秒级，串行化不会成为瓶颈。

    execute() 直接返回 fetchall() 的结果而不返回 Cursor，这一点很关键：
    Cursor 只有在被真正迭代时才与连接交互，若把 Cursor 传到锁外面再
    for 循环，锁就形同虚设。返回物化的行列表能保证「取数」这个动作
    完整地待在锁内。
    """

    def __init__(self, conn):
        self._conn = conn
        self._lock = threading.RLock()

    def execute(self, sql, args=()):
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    def execute_one(self, sql, args=()):
        with self._lock:
            return self._conn.execute(sql, args).fetchone()

    def executescript(self, sql):
        with self._lock:
            self._conn.executescript(sql)

    def commit(self):
        with self._lock:
            self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()


class Vault:
    def __init__(self, root: str):
        self.root = root
        self.chunks_dir = os.path.join(root, "chunks")
        self.snapshots_dir = os.path.join(root, "snapshots")
        self.localcopies_dir = os.path.join(root, "localcopies")
        self.db_path = os.path.join(root, "index.db")
        self.config_path = os.path.join(root, "config.json")
        for d in (self.chunks_dir, self.snapshots_dir, self.localcopies_dir):
            os.makedirs(d, exist_ok=True)
        self.store = ChunkStore(self.chunks_dir)
        # check_same_thread=False + _LockedDB：连接由主线程创建，
        # 但所有查询都跑在 QThreadPool 的工作线程里。
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        self.db = _LockedDB(conn)
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self):
        try:
            self.db.close()
        except Exception:
            pass

    # ==========================================================
    # 游戏管理
    # ==========================================================
    def add_game(self, name: str, paths, platform: str = "", note: str = "") -> Game:
        paths = [os.path.abspath(p) for p in paths]
        g = Game(id=new_id("game|" + name), name=name, paths=paths,
                 platform=platform, note=note, created_at=time.time())
        self.save_game(g)
        return g

    def save_game(self, g: Game):
        self.db.execute(
            "INSERT OR REPLACE INTO games(id,name,paths,platform,auto_scan,created_at,note)"
            " VALUES(?,?,?,?,?,?,?)",
            (g.id, g.name, json.dumps(g.paths, ensure_ascii=False), g.platform,
             int(g.auto_scan), g.created_at, g.note))
        self.db.commit()

    def remove_game(self, game_id: str, keep_snapshots: bool = True):
        self.db.execute("DELETE FROM games WHERE id=?", (game_id,))
        if not keep_snapshots:
            for s in self.list_snapshots(game_id):
                self.delete_snapshot(s.id)
        self.db.commit()

    def get_game(self, game_id: str) -> Game:
        r = self.db.execute_one("SELECT * FROM games WHERE id=?", (game_id,))
        if not r:
            raise KeyError("游戏不存在: %s" % game_id)
        return Game(id=r["id"], name=r["name"], paths=json.loads(r["paths"]),
                    platform=r["platform"], auto_scan=bool(r["auto_scan"]),
                    created_at=r["created_at"], note=r["note"])

    def list_games(self) -> list:
        out = []
        for r in self.db.execute("SELECT * FROM games ORDER BY name"):
            out.append(Game(id=r["id"], name=r["name"], paths=json.loads(r["paths"]),
                            platform=r["platform"], auto_scan=bool(r["auto_scan"]),
                            created_at=r["created_at"], note=r["note"]))
        return out

    # ==========================================================
    # 快照：扫描 + 分块 + 去重落盘
    # ==========================================================
    def scan_local(self, game: Game, progress_cb=None, quick_hint=None) -> list:
        """扫描本地存档目录，返回 FileEntry 列表（chunks 已写入本地 store）。

        虚拟根规则：多个存档目录时，路径前缀为 `目录序号/`，保证无损还原。

        quick_hint: {相对路径: 上一次快照里的 FileEntry}。给定时走快速
            路径 —— 文件 size 与 mtime 都没变（容差 2 秒）且引用的 chunk
            在本地 store 里都还在，就直接复用旧 FileEntry，**完全不读文件
            内容、不算哈希、不压缩**。状态刷新每小时跑一次、每次开机也跑，
            全量重算对几十上百 MB 的存档是无谓的 IO+CPU。
            注意：mtime 不可信（Cemu 虚拟时钟），所以快速路径只能用于
            「展示状态」，真正的同步计划 SyncEngine.plan() 仍然全量精算，
            判定依据永远是内容哈希。
        """
        entries = []
        quick_hint = quick_hint or {}
        total_files = 0
        for p in game.paths:
            if os.path.isfile(p):
                total_files += 1
            elif os.path.isdir(p):
                for r, _, fs in os.walk(p):
                    total_files += len(fs)
        done = 0
        for idx, root in enumerate(game.paths):
            prefix = "" if len(game.paths) == 1 else "%d/" % idx
            if os.path.isfile(root):
                files = [(os.path.dirname(root), os.path.basename(root))]
                base = os.path.dirname(root)
            elif os.path.isdir(root):
                files = []
                base = root
                for r, _, fs in os.walk(root):
                    for f in fs:
                        files.append((r, f))
            else:
                continue
            for r, f in files:
                full = os.path.join(r, f)
                rel = prefix + os.path.relpath(full, base).replace(os.sep, "/")
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                hint = quick_hint.get(rel)
                # 快速路径：size/mtime 未变且 chunk 实体都在，直接复用旧记录，
                # 不读文件内容。mtime 给 2 秒容差，兼容不同文件系统的时间粒度。
                if (hint is not None and hint.size == st.st_size
                        and abs(hint.mtime - st.st_mtime) <= 2.0
                        and hint.chunks
                        and all(self.store.has(c.sha256) for c in hint.chunks)):
                    entries.append(FileEntry(
                        path=rel, sha256=hint.sha256, size=st.st_size,
                        mtime=st.st_mtime, chunks=list(hint.chunks)))
                else:
                    chunks, file_sha = chunker.chunk_file(full)
                    for c in chunks:
                        self.store.put(c["sha256"], c["blob"])
                    entries.append(FileEntry(
                        path=rel, sha256=file_sha, size=st.st_size, mtime=st.st_mtime,
                        chunks=[ChunkRef(sha256=c["sha256"], size=c["size"])
                                for c in chunks]))
                done += 1
                if progress_cb:
                    progress_cb(done, max(total_files, 1))
        return entries

    def create_snapshot(self, game: Game, kind: str = "manual",
                        note: str = "", progress_cb=None) -> Snapshot:
        entries = self.scan_local(game, progress_cb)
        snap = Snapshot(id=new_id("snap|%s|%f" % (game.id, time.time())),
                        game_id=game.id, created_at=time.time(), kind=kind,
                        files=entries,
                        total_size=sum(e.size for e in entries), note=note)
        self._write_snapshot(snap)
        return snap

    def _write_snapshot(self, snap: Snapshot):
        self.db.execute(
            "INSERT OR REPLACE INTO snapshots(id,game_id,created_at,kind,total_size,note)"
            " VALUES(?,?,?,?,?,?)",
            (snap.id, snap.game_id, snap.created_at, snap.kind, snap.total_size, snap.note))
        self.db.execute("DELETE FROM files WHERE snapshot_id=?", (snap.id,))
        self.db.execute("DELETE FROM chunk_refs WHERE snapshot_id=?", (snap.id,))
        for e in snap.files:
            self.db.execute(
                "INSERT OR REPLACE INTO files(snapshot_id,game_id,path,sha256,size,mtime,chunks)"
                " VALUES(?,?,?,?,?,?,?)",
                (snap.id, snap.game_id, e.path, e.sha256, e.size, e.mtime,
                 json.dumps([{"sha256": c.sha256, "size": c.size} for c in e.chunks])))
            for c in e.chunks:
                self.db.execute(
                    "INSERT OR IGNORE INTO chunk_refs(sha,snapshot_id) VALUES(?,?)",
                    (c.sha256, snap.id))
        self.db.commit()
        # 清单同样原子写：先写 .tmp 再 replace，避免进程被杀 / 断电时
        # 留下半截 JSON（list_snapshots 会逐个 json.load，坏文件会让该
        # 游戏的云端快照列表永久缺一份）。
        sp = os.path.join(self.snapshots_dir, snap.id + ".json")
        stmp = sp + ".tmp"
        with open(stmp, "w", encoding="utf-8") as f:
            json.dump(snap.to_dict(), f, ensure_ascii=False, indent=1)
        os.replace(stmp, sp)

    def get_snapshot(self, sid: str) -> Snapshot:
        p = os.path.join(self.snapshots_dir, sid + ".json")
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                return Snapshot.from_dict(json.load(f))
        r = self.db.execute_one("SELECT * FROM snapshots WHERE id=?", (sid,))
        if not r:
            raise KeyError("快照不存在: %s" % sid)
        return self._snapshot_from_db(r)

    def _snapshot_from_db(self, r) -> Snapshot:
        files = []
        for fr in self.db.execute(
                "SELECT * FROM files WHERE snapshot_id=? ORDER BY path", (r["id"],)):
            chunks = [ChunkRef(sha256=c["sha256"], size=c["size"])
                      for c in json.loads(fr["chunks"])]
            files.append(FileEntry(path=fr["path"], sha256=fr["sha256"],
                                   size=fr["size"], mtime=fr["mtime"],
                                   chunks=chunks))
        return Snapshot(id=r["id"], game_id=r["game_id"], created_at=r["created_at"],
                        kind=r["kind"], files=files, total_size=r["total_size"],
                        note=r["note"])

    def list_snapshots(self, game_id: str) -> list:
        out = []
        for r in self.db.execute(
                "SELECT * FROM snapshots WHERE game_id=? ORDER BY created_at DESC",
                (game_id,)):
            out.append(self._snapshot_from_db(r))
        return out

    def latest_snapshot(self, game_id: str):
        s = self.list_snapshots(game_id)
        return s[0] if s else None

    def delete_snapshot(self, sid: str):
        self.db.execute("DELETE FROM snapshots WHERE id=?", (sid,))
        self.db.execute("DELETE FROM files WHERE snapshot_id=?", (sid,))
        self.db.execute("DELETE FROM chunk_refs WHERE snapshot_id=?", (sid,))
        self.db.commit()
        p = os.path.join(self.snapshots_dir, sid + ".json")
        if os.path.isfile(p):
            os.remove(p)

    def prune_snapshots(self, game_id: str, keep: int) -> int:
        """只保留最近的 keep 个快照，其余删除。返回删除数。

        配置里的「每款游戏保留快照数」此前只是个摆设 —— 没有任何代码读它。
        定时同步一小时一次，不裁剪的话一年就是近九千份快照清单，
        每次 list_snapshots 都要把它们全部 json.load 一遍。

        两个保护：
        1. 当前同步基准（sync_state.base_snapshot_id）不删。它是三方合并的
           参照物，删了会让下次同步退化成「拿云端倒数第二版当基准」，
           在云端也被裁剪过的场景下直接丢掉参照。
        2. 删完跑一次 gc，把因此失去引用的 chunk 收掉。
        """
        if keep is None or keep <= 0:
            return 0
        snaps = self.list_snapshots(game_id)          # created_at 倒序
        ss = self.get_sync_state(game_id) or {}
        base_id = ss.get("base_snapshot_id")
        removed = 0
        for s in snaps[keep:]:
            if s.id == base_id:
                continue
            self.delete_snapshot(s.id)
            removed += 1
        if removed:
            self.gc()
        return removed

    # ==========================================================
    # 重复快照合并
    # ==========================================================
    @staticmethod
    def manifest_fingerprint(snap: Snapshot) -> str:
        """快照的内容指纹：文件清单逐字节等价即为「完全相同的备份」。

        纳入判定：每个文件的 (相对路径, 内容 sha256, 大小, 分块序列)。
        不纳入：created_at / kind / note / 快照 id / mtime ——
        前者是元数据，mtime 会因游戏「读了但没改」而抖动，
        内容哈希不变就没有合并的意义外的干扰。
        """
        parts = []
        for e in sorted(snap.files, key=lambda x: x.path):
            chunks = ",".join("%s:%d" % (c.sha256, c.size) for c in e.chunks)
            parts.append("%s|%s|%d|%s" % (e.path, e.sha256, e.size, chunks))
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()

    def merge_duplicate_snapshots(self, game_id: str) -> int:
        """合并内容完全相同的快照，返回删除数。

        场景：连着点几次「立即备份」、定时备份每小时跑一轮而存档没变 ——
        chunk 因为内容寻址天然共享，但快照清单会一份份堆着，
        历史列表里出现一排时间不同、内容相同的条目，选哪份回滚都一样。

        保留组内最新的一份（created_at 最大），删除其余。
        保护：sync_state.base_snapshot_id 指向的快照不删 —— 它是三方
        合并的参照物，与 prune_snapshots 同款保护。
        """
        snaps = self.list_snapshots(game_id)          # created_at 倒序
        if len(snaps) < 2:
            return 0
        ss = self.get_sync_state(game_id) or {}
        base_id = ss.get("base_snapshot_id")
        seen = set()
        removed = 0
        for s in snaps:
            fp = self.manifest_fingerprint(s)
            if fp in seen:
                if s.id != base_id:
                    self.delete_snapshot(s.id)
                    removed += 1
            else:
                seen.add(fp)
        if removed:
            self.gc()
        return removed

    # ==========================================================
    # 还原（覆盖前自动留底）
    # ==========================================================
    def restore_snapshot(self, game: Game, sid: str, safety: bool = True,
                         progress_cb=None, only_paths=None) -> dict:
        """把快照还原到游戏目录。

        safety=True 时，每个将被覆盖/删除的现有文件先复制到 localcopies/ 留底。
        only_paths 给定则只还原这些相对路径（用于单文件回滚）。
        返回 {"restored": n, "backed_up": n, "skipped": n, "bytes": b}
        """
        snap = self.get_snapshot(sid)
        targets = {e.path: e for e in snap.files}
        if only_paths:
            targets = {p: targets[p] for p in only_paths if p in targets}

        stats = {"restored": 0, "backed_up": 0, "skipped": 0, "bytes": 0}
        total = max(len(targets), 1)
        done = 0

        # 目标根目录：优先用当前配置的路径，快照里记录的历史路径作为兜底
        roots = self._roots_for(game, snap)

        for rel, e in targets.items():
            dst = self._resolve_target(roots, rel)
            if dst is None:
                stats["skipped"] += 1
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if safety and os.path.isfile(dst):
                self._backup_existing(game.id, rel, dst, reason="restore:%s" % sid)
                stats["backed_up"] += 1
            with open(dst, "wb") as out:
                for c in e.chunks:
                    out.write(self.store.read_decompressed(c.sha256))
            stats["restored"] += 1
            stats["bytes"] += e.size
            done += 1
            if progress_cb:
                progress_cb(done, total)

        # 快照里没有、但本地存在的文件：不动。删档是危险操作，交给用户手动决定。
        return stats

    def _roots_for(self, game: Game, snap: Snapshot) -> list:
        """把快照的虚拟根映射回真实目录。

        多目录时 rel 形如 `0/xxx`、`1/yyy`；单目录时无前缀。
        """
        roots = list(game.paths)
        if len(roots) == 1:
            return [roots[0]]
        return roots

    def _resolve_target(self, roots: list, rel: str):
        """虚拟根路径解析。陈旧的序号前缀（超出当前目录数）返回 None 跳过；
        序号有效但路径试图越界则抛 UnsafePathError，不静默写出去。"""
        if len(roots) == 1:
            return safe_join(roots[0], rel)
        parts = rel.split("/", 1)
        if len(parts) != 2 or not parts[0].isdigit():
            return None
        idx = int(parts[0])
        if idx >= len(roots):
            return None
        return safe_join(roots[idx], parts[1])

    def _backup_existing(self, game_id: str, rel: str, src: str, reason: str):
        stamp = time.strftime("%Y%m%d_%H%M%S")
        safe_rel = rel.replace("/", "__").replace("\\", "__")
        dst = os.path.join(self.localcopies_dir,
                           "%s_%s_%s" % (game_id[:8], stamp, safe_rel))
        shutil.copy2(src, dst)
        self.db.execute(
            "INSERT INTO local_copies(game_id,path,backup_path,created_at,reason)"
            " VALUES(?,?,?,?,?)", (game_id, rel, dst, time.time(), reason))
        self.db.commit()

    def list_local_copies(self, game_id: str = None) -> list:
        q = "SELECT * FROM local_copies"
        args = ()
        if game_id:
            q += " WHERE game_id=?"
            args = (game_id,)
        q += " ORDER BY created_at DESC"
        return [dict(r) for r in self.db.execute(q, args)]

    def restore_local_copy(self, copy_id: int) -> str:
        r = self.db.execute_one("SELECT * FROM local_copies WHERE id=?",
                                (copy_id,))
        if not r:
            raise KeyError("留底记录不存在")
        return r["backup_path"]

    # ==========================================================
    # 同步基准
    # ==========================================================
    def get_sync_state(self, game_id: str):
        r = self.db.execute_one("SELECT * FROM sync_state WHERE game_id=?",
                                (game_id,))
        return dict(r) if r else None

    def set_sync_state(self, game_id: str, base_snapshot_id: str = None,
                       last_sync_at: float = None, last_error: str = None):
        cur = self.get_sync_state(game_id) or {}
        self.db.execute(
            "INSERT OR REPLACE INTO sync_state(game_id,base_snapshot_id,last_sync_at,last_error)"
            " VALUES(?,?,?,?)",
            (game_id,
             base_snapshot_id if base_snapshot_id is not None else cur.get("base_snapshot_id"),
             last_sync_at if last_sync_at is not None else cur.get("last_sync_at", 0.0),
             last_error if last_error is not None else cur.get("last_error")))
        self.db.commit()

    # ==========================================================
    # 维护
    # ==========================================================
    def referenced_chunks(self) -> set:
        return {r[0] for r in self.db.execute("SELECT DISTINCT sha FROM chunk_refs")}

    def gc(self) -> int:
        return self.store.gc(self.referenced_chunks())

    def stats(self) -> dict:
        return {
            "games": len(self.list_games()),
            "snapshots": self.db.execute_one("SELECT COUNT(*) FROM snapshots")[0],
            "chunks": self.store.count(),
            "chunk_bytes": self.store.total_size(),
            "local_copies": self.db.execute_one(
                "SELECT COUNT(*) FROM local_copies")[0],
        }
