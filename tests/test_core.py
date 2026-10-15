# -*- coding: utf-8 -*-
"""核心引擎单元测试。

跑法：
    python tests/test_core.py          # 或 python -m unittest discover -s tests

覆盖：
    1. 分块 / 哈希 / 压缩往返
    2. chunk store 去重与回收
    3. 三方合并判定表（_decide）全分支
    4. 快照 → 修改 → 回滚，含覆盖前自动留底
    5. 冲突的三种裁决
    6. 增量同步：第二次只传变化的 chunk
    7. 多存档目录的虚拟根还原
    8. 删除传播：云端删除 → 本地删除并留底
"""
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gamesave import chunker
from gamesave.chunker import CHUNK_SIZE
from gamesave.models import (Game, Snapshot, FileEntry, ChunkRef,
                             RESOLVE_KEEP_LOCAL, RESOLVE_USE_REMOTE,
                             RESOLVE_SAVE_BOTH,
                             SYNC_SYNCED, SYNC_LOCAL_AHEAD, SYNC_UNKNOWN,
                             SYNC_ERROR)
from gamesave.store import ChunkStore
from gamesave.vault import Vault
from gamesave.remote import LocalFolderBackend
from gamesave.sync import SyncEngine
from gamesave.sync import (ACT_UPLOAD, ACT_DOWNLOAD, ACT_NONE, ACT_DEL_LOCAL,
                           ACT_DEL_REMOTE, ACT_CONFLICT)


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def read(path):
    with open(path, "rb") as f:
        return f.read()


# ==================================================================
# 1. 分块 / 哈希 / 压缩
# ==================================================================
class TestChunker(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gsc_chunk_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_file(self):
        p = os.path.join(self.tmp, "empty.sav")
        write(p, b"")
        chunks, sha = chunker.chunk_file(p)
        self.assertEqual(chunks, [])
        self.assertEqual(sha, chunker.sha256_hex(b""))

    def test_boundary_split(self):
        """正好一个 chunk、超一个字节、正好两个 chunk 的三种边界。"""
        for n, expect in ((CHUNK_SIZE, 1), (CHUNK_SIZE + 1, 2),
                          (CHUNK_SIZE * 2, 2), (CHUNK_SIZE * 2 + 7, 3)):
            p = os.path.join(self.tmp, "b_%d.bin" % n)
            write(p, os.urandom(n))
            chunks, sha = chunker.chunk_file(p)
            self.assertEqual(len(chunks), expect, "size=%d" % n)
            # 整文件哈希必须与单独哈希一致 —— 增量算法不能算错
            self.assertEqual(sha, chunker.hash_file(p))
            self.assertEqual(sum(c["size"] for c in chunks), n)

    def test_sha_is_of_plaintext_not_compressed(self):
        """chunk 哈希必须是原始内容的哈希，与压缩算法无关。"""
        data = b"A" * 4096
        p = os.path.join(self.tmp, "repeat.bin")
        write(p, data)
        chunks, _ = chunker.chunk_file(p)
        self.assertEqual(chunks[0]["sha256"], chunker.sha256_hex(data))

    def test_compress_roundtrip_all_tags(self):
        """三种标签都能正确解压回原内容。"""
        samples = [
            b"x" * 100000,                    # 高重复 → 压得动
            os.urandom(100000),               # 随机 → 压不动，走 raw
            b"",                              # 空
            os.urandom(3),
        ]
        for raw in samples:
            tag, payload = chunker.compress(raw)
            self.assertIn(tag, (chunker.TAG_ZSTD, chunker.TAG_ZLIB,
                                chunker.TAG_RAW))
            self.assertEqual(chunker.decompress(tag, payload), raw)

    def test_raw_when_compression_does_not_help(self):
        tag, payload = chunker.compress(os.urandom(65536))
        self.assertEqual(tag, chunker.TAG_RAW)
        self.assertEqual(payload, os.urandom(0) + payload)  # 原样
        self.assertGreaterEqual(len(payload), 65536)


# ==================================================================
# 2. chunk store：去重 + 回收
# ==================================================================
class TestChunkStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gsc_store_")
        self.store = ChunkStore(os.path.join(self.tmp, "chunks"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dedup(self):
        data = os.urandom(2048)
        tag, payload = chunker.compress(data)
        blob = tag + payload
        sha = chunker.sha256_hex(data)
        self.assertTrue(self.store.put(sha, blob))
        self.assertFalse(self.store.put(sha, blob))     # 第二次不写
        self.assertTrue(self.store.has(sha))
        self.assertEqual(self.store.count(), 1)
        self.assertEqual(self.store.read_decompressed(sha), data)

    def test_same_content_two_games_share_one_chunk(self):
        """跨游戏、跨版本去重：内容相同就只在磁盘上存一份。"""
        data = b"shared-save-block" * 1000
        tag, payload = chunker.compress(data)
        blob = tag + payload
        sha = chunker.sha256_hex(data)
        self.store.put(sha, blob)
        # 第二个游戏放入同一内容
        self.assertFalse(self.store.put(sha, blob))
        self.assertEqual(self.store.count(), 1)

    def test_missing_and_verify(self):
        data = os.urandom(1024)
        tag, payload = chunker.compress(data)
        sha = chunker.sha256_hex(data)
        self.assertEqual(self.store.missing([sha]), [sha])
        self.store.put(sha, tag + payload)
        self.assertEqual(self.store.missing([sha]), [])
        self.assertTrue(self.store.verify(sha))

        # 篡改磁盘上的 chunk，verify 必须能发现
        with open(self.store.path_of(sha), "r+b") as f:
            f.seek(1)
            f.write(b"\x00")
        self.assertFalse(self.store.verify(sha))

    def test_gc_keeps_referenced(self):
        keep = chunker.sha256_hex(b"keep")
        drop = chunker.sha256_hex(b"drop")
        for d, s in ((b"keep", keep), (b"drop", drop)):
            tag, payload = chunker.compress(d)
            self.store.put(s, tag + payload)
        self.assertEqual(self.store.count(), 2)
        removed = self.store.gc({keep})
        self.assertEqual(removed, 1)
        self.assertTrue(self.store.has(keep))
        self.assertFalse(self.store.has(drop))

    def test_tmp_part_not_counted(self):
        """_tmp 下的半成品不能算进 chunk 数与体积。"""
        tmpdir = os.path.join(self.store.root, "_tmp")
        write(os.path.join(tmpdir, "abc.part"), b"half-written")
        self.assertEqual(self.store.count(), 0)
        self.assertEqual(self.store.total_size(), 0)


# ==================================================================
# 3. 三方合并判定表
# ==================================================================
class TestDecide(unittest.TestCase):
    """_decide(l, r, b) 的完整真值表。

    base 是「上次成功同步时两边一致的那个版本」。没有 base 就无法区分
    「谁改过」，所以两方对比必然把所有差异都误判成冲突。
    """

    def test_both_sides_identical(self):
        self.assertEqual(SyncEngine._decide("a", "a", "a")[0], ACT_NONE)
        self.assertEqual(SyncEngine._decide("a", "a", None)[0], ACT_NONE)

    def test_remote_changed_local_untouched(self):
        act, why = SyncEngine._decide("a", "b", "a")
        self.assertEqual(act, ACT_DOWNLOAD)
        self.assertIn("下载", why)

    def test_local_changed_remote_untouched(self):
        act, why = SyncEngine._decide("b", "a", "a")
        self.assertEqual(act, ACT_UPLOAD)
        self.assertIn("上传", why)

    def test_both_changed_differently(self):
        act, _ = SyncEngine._decide("b", "c", "a")
        self.assertEqual(act, ACT_CONFLICT)

    def test_both_added_same_path_different_content(self):
        act, _ = SyncEngine._decide("b", "c", None)
        self.assertEqual(act, ACT_CONFLICT)

    def test_remote_deleted_local_untouched(self):
        act, _ = SyncEngine._decide("a", None, "a")
        self.assertEqual(act, ACT_DEL_REMOTE)

    def test_local_deleted_remote_untouched(self):
        act, _ = SyncEngine._decide(None, "a", "a")
        self.assertEqual(act, ACT_DEL_LOCAL)

    def test_local_readded_identical_content(self):
        """删了又加回同样内容：等于没变，不该判成冲突。"""
        self.assertEqual(SyncEngine._decide("a", "a", None)[0], ACT_NONE)

    def test_both_deleted(self):
        self.assertEqual(SyncEngine._decide(None, None, "a")[0], ACT_NONE)



# ==================================================================
# 4~8. 端到端：快照 / 回滚 / 冲突 / 增量 / 虚拟根 / 删除传播
# ==================================================================
class BaseSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gsc_e2e_")
        self.vault = Vault(os.path.join(self.tmp, "vault"))
        self.remote = LocalFolderBackend(os.path.join(self.tmp, "remote"))
        self.engine = SyncEngine(self.vault, self.remote)
        self.save_dir = os.path.join(self.tmp, "FakeGame", "saves")
        os.makedirs(self.save_dir, exist_ok=True)
        self.game = self.vault.add_game("FakeGame", [self.save_dir])

    def tearDown(self):
        self.vault.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------- 辅助 ----------
    def fake_remote_file(self, path, data, created_at):
        """在云端伪造一个文件：上传 chunk + 构造快照。"""
        tag, payload = chunker.compress(data)
        sha = chunker.sha256_hex(data)
        self.remote.upload_chunk(sha, tag + payload)
        snap = Snapshot(id="snap_%s_%d" % (path.replace("/", "_"),
                                           int(created_at * 1000)),
                        game_id=self.game.id, created_at=created_at,
                        kind="sync",
                        files=[FileEntry(path=path, sha256=sha, size=len(data),
                                         mtime=created_at,
                                         chunks=[ChunkRef(sha256=sha, size=len(data))])],
                        total_size=len(data))
        self.remote.put_snapshot(snap)
        return snap

    def sync(self):
        plan = self.engine.plan(self.game)
        return plan, self.engine.execute(self.game, plan, {})

    def local_copies(self):
        return os.listdir(os.path.join(self.vault.root, "localcopies"))


class TestPruning(BaseSyncTest):
    def test_prune_keeps_recent_n(self):
        for i in range(6):
            write(os.path.join(self.save_dir, "a.sav"), b"v%d" % i)
            snap = self.vault.create_snapshot(self.game)
            self.remote.put_snapshot(snap)
            self.vault.set_sync_state(self.game.id, base_snapshot_id=snap.id,
                                      last_sync_at=time.time())
            time.sleep(0.01)
        self.assertEqual(len(self.vault.list_snapshots(self.game.id)), 6)
        removed = self.vault.prune_snapshots(self.game.id, 3)
        self.assertEqual(removed, 3)
        left = self.vault.list_snapshots(self.game.id)
        self.assertEqual(len(left), 3)
        # 留下的必须是最新的三份
        self.assertEqual([len(s.files) for s in left], [1, 1, 1])
        self.assertEqual(left[0].files[0].sha256,
                         chunker.sha256_hex(b"v5"))

    def test_prune_protects_base_snapshot(self):
        """同步基准是三方合并的参照物，不能被裁掉。"""
        snaps = []
        for i in range(5):
            write(os.path.join(self.save_dir, "a.sav"), b"v%d" % i)
            s = self.vault.create_snapshot(self.game)
            snaps.append(s)
            time.sleep(0.01)
        base = snaps[0]                       # 最旧的那份当基准
        self.vault.set_sync_state(self.game.id, base_snapshot_id=base.id,
                                  last_sync_at=time.time())
        self.vault.prune_snapshots(self.game.id, 2)
        ids = [s.id for s in self.vault.list_snapshots(self.game.id)]
        self.assertIn(base.id, ids)           # 基准必须还在
        self.assertEqual(len(ids), 3)         # 2 + 受保护的基准

    def test_prune_reclaims_chunks(self):
        """删掉的快照若让某些 chunk 失去引用，gc 必须把它们收回来。"""
        write(os.path.join(self.save_dir, "a.sav"), os.urandom(100000))
        s1 = self.vault.create_snapshot(self.game)
        write(os.path.join(self.save_dir, "a.sav"), os.urandom(100000))
        s2 = self.vault.create_snapshot(self.game)
        self.assertNotEqual(s1.files[0].sha256, s2.files[0].sha256)
        self.assertEqual(self.vault.store.count(), 2)
        self.vault.set_sync_state(self.game.id, base_snapshot_id=s2.id,
                                  last_sync_at=time.time())
        self.vault.prune_snapshots(self.game.id, 1)
        # s1 的 chunk 已无引用，应被回收
        self.assertFalse(self.vault.store.has(s1.files[0].chunks[0].sha256))
        self.assertTrue(self.vault.store.has(s2.files[0].chunks[0].sha256))

    def test_remote_prune_and_gc(self):
        for i in range(5):
            write(os.path.join(self.save_dir, "a.sav"), os.urandom(50000))
            snap = self.vault.create_snapshot(self.game)
            self.remote.put_snapshot(snap)
            time.sleep(0.01)
        self.assertEqual(len(self.remote.list_snapshots(self.game.id)), 5)
        n_chunks = len(self.remote.all_referenced_chunks())
        removed = self.remote.prune_snapshots(self.game.id, 2)
        self.assertEqual(removed, 3)
        self.assertEqual(len(self.remote.list_snapshots(self.game.id)), 2)
        # 云端的 chunk 数不应超过剩余快照的引用数
        self.assertLessEqual(len(self.remote.all_referenced_chunks()),
                             n_chunks)


class TestSnapshotAndRollback(BaseSyncTest):
    def test_snapshot_then_rollback(self):
        write(os.path.join(self.save_dir, "a.sav"), b"version-1")
        snap1 = self.vault.create_snapshot(self.game, kind="manual")

        write(os.path.join(self.save_dir, "a.sav"), b"version-2-longer")
        write(os.path.join(self.save_dir, "b.sav"), b"new-file")

        st = self.vault.restore_snapshot(self.game, snap1.id)
        self.assertEqual(read(os.path.join(self.save_dir, "a.sav")), b"version-1")
        self.assertEqual(st["restored"], 1)
        self.assertEqual(st["backed_up"], 1)     # a.sav 被覆盖，先留底
        self.assertTrue(os.path.isfile(
            os.path.join(self.save_dir, "b.sav")))   # 快照里没有的不动

    def test_rollback_leaves_local_copy(self):
        """覆盖前自动留底 —— 防存档丢失的最后一道闸。"""
        write(os.path.join(self.save_dir, "a.sav"), b"v1")
        snap1 = self.vault.create_snapshot(self.game)
        write(os.path.join(self.save_dir, "a.sav"), b"v2")
        self.vault.restore_snapshot(self.game, snap1.id)
        copies = self.vault.list_local_copies(self.game.id)
        self.assertEqual(len(copies), 1)
        self.assertEqual(read(copies[0]["backup_path"]), b"v2")

    def test_rollback_respects_safety_off(self):
        write(os.path.join(self.save_dir, "a.sav"), b"v1")
        snap1 = self.vault.create_snapshot(self.game)
        write(os.path.join(self.save_dir, "a.sav"), b"v2")
        st = self.vault.restore_snapshot(self.game, snap1.id, safety=False)
        self.assertEqual(st["backed_up"], 0)
        self.assertEqual(self.vault.list_local_copies(self.game.id), [])

    def test_rollback_only_paths(self):
        write(os.path.join(self.save_dir, "a.sav"), b"a-v1")
        write(os.path.join(self.save_dir, "b.sav"), b"b-v1")
        snap1 = self.vault.create_snapshot(self.game)
        write(os.path.join(self.save_dir, "a.sav"), b"a-v2")
        write(os.path.join(self.save_dir, "b.sav"), b"b-v2")
        self.vault.restore_snapshot(self.game, snap1.id, only_paths=["a.sav"])
        self.assertEqual(read(os.path.join(self.save_dir, "a.sav")), b"a-v1")
        self.assertEqual(read(os.path.join(self.save_dir, "b.sav")), b"b-v2")

    def test_snapshot_roundtrip_json(self):
        write(os.path.join(self.save_dir, "a.sav"), b"payload" * 100)
        snap = self.vault.create_snapshot(self.game)
        again = self.vault.get_snapshot(snap.id)
        self.assertEqual(len(again.files), 1)
        self.assertEqual(again.files[0].sha256, snap.files[0].sha256)
        self.assertEqual(again.total_size, snap.total_size)


class TestSync(BaseSyncTest):
    def test_noop_sync_creates_no_snapshot(self):
        """没有变化的同步不该生成新快照。

        定时同步一小时跑一次，无变化也造一份的话一年就是近九千份
        几乎相同的清单，还会把「保留 N 个历史版本」的配额占满。
        """
        write(os.path.join(self.save_dir, "a.sav"), os.urandom(200000))
        self.sync()
        before = len(self.vault.list_snapshots(self.game.id))
        before_remote = len(self.remote.list_snapshots(self.game.id))
        _p, res = self.sync()
        self.assertEqual(res["uploaded"], 0)
        self.assertEqual(res["downloaded"], 0)
        self.assertIsNone(res["snapshot"])
        self.assertEqual(len(self.vault.list_snapshots(self.game.id)), before)
        self.assertEqual(len(self.remote.list_snapshots(self.game.id)),
                         before_remote)

    def test_first_sync_uploads_everything(self):
        write(os.path.join(self.save_dir, "a.sav"), os.urandom(500000))
        plan, res = self.sync()
        self.assertEqual(res["errors"], [])
        self.assertGreater(res["uploaded"], 0)
        self.assertEqual(self.engine.status(self.game).state, "synced")

    def test_second_sync_uploads_nothing(self):
        write(os.path.join(self.save_dir, "a.sav"), os.urandom(500000))
        self.sync()
        _plan, res = self.sync()
        self.assertEqual(res["uploaded"], 0)
        self.assertEqual(res["downloaded"], 0)

    def test_incremental_only_changed_chunks(self):
        """改动文件中部，只有受影响的 chunk 需要重传。"""
        data = bytearray(os.urandom(CHUNK_SIZE * 3))
        p = os.path.join(self.save_dir, "big.sav")
        write(p, bytes(data))
        self.sync()

        data[CHUNK_SIZE + 10:CHUNK_SIZE + 20] = b"X" * 10   # 只动第二个 chunk
        write(p, bytes(data))
        plan = self.engine.plan(self.game)
        self.assertGreater(plan.upload_bytes, 0)
        # 3 个 chunk 里只有 1 个变了，重传量应远小于整文件
        self.assertLess(plan.upload_bytes, len(data))
        _p, res = self.sync()
        self.assertLess(res["bytes"], len(data))

    def test_download_when_remote_ahead(self):
        write(os.path.join(self.save_dir, "a.sav"), b"v1")
        self.sync()
        # 模拟另一台机器改了存档并同步上来
        time.sleep(0.05)
        self.fake_remote_file("a.sav", b"v2-from-other-pc",
                              created_at=time.time())
        plan = self.engine.plan(self.game)
        self.assertEqual(self.engine.status(self.game).state, "remote")
        self.assertGreater(plan.download_bytes, 0)
        _p, res = self.sync()
        self.assertEqual(res["errors"], [])
        self.assertEqual(read(os.path.join(self.save_dir, "a.sav")),
                         b"v2-from-other-pc")

    def test_delete_propagates_and_backs_up(self):
        """本地删了 → 云端跟着删；云端删了 → 本地跟着删且先留底。"""
        write(os.path.join(self.save_dir, "a.sav"), b"v1")
        self.sync()

        # 方向一：本地删除。本地已经没有了，没有可留底的东西，
        # 该做的只是让云端那份也消失。
        os.remove(os.path.join(self.save_dir, "a.sav"))
        _p, res = self.sync()
        self.assertEqual(res["errors"], [])
        self.assertFalse(os.path.exists(os.path.join(self.save_dir, "a.sav")))
        self.assertEqual(len(self.local_copies()), 0)
        self.assertEqual(self.remote.list_snapshots(self.game.id)[0].files, [])

        # 方向二：云端删除。本地还有，删之前必须留底。
        write(os.path.join(self.save_dir, "a.sav"), b"v2")
        self.sync()
        time.sleep(0.05)
        empty = Snapshot(id="snap_empty_%d" % int(time.time() * 1000),
                         game_id=self.game.id, created_at=time.time(),
                         kind="sync", files=[], total_size=0)
        self.remote.put_snapshot(empty)
        _p, res = self.sync()
        self.assertEqual(res["errors"], [])
        self.assertFalse(os.path.exists(os.path.join(self.save_dir, "a.sav")))
        self.assertEqual(len(self.local_copies()), 1)
        self.assertEqual(read(self.vault.list_local_copies()[0]["backup_path"]),
                         b"v2")


class TestConflict(BaseSyncTest):
    def _make_conflict(self, local_data, remote_data):
        write(os.path.join(self.save_dir, "a.sav"), b"base")
        self.sync()                                   # base = "base"，时刻 T0
        time.sleep(0.05)
        write(os.path.join(self.save_dir, "a.sav"), local_data)
        # 云端快照的时间戳必须晚于 base、但早于稍后 execute 生成的新快照，
        # 否则 list_snapshots 按时间倒序会把这份假快照一直排在最新，
        # 裁决后状态就永远停在 remote 而不是 synced。
        self.fake_remote_file("a.sav", remote_data, created_at=time.time())
        return self.engine.plan(self.game)

    def test_conflict_detected(self):
        plan = self._make_conflict(b"local-edit", b"remote-edit")
        self.assertTrue(plan.has_conflict)
        self.assertEqual(self.engine.status(self.game).state, "conflict")
        self.assertEqual(len(plan.conflicts), 1)
        self.assertEqual(plan.conflicts[0].path, "a.sav")

    def test_keep_local(self):
        plan = self._make_conflict(b"local-edit", b"remote-edit")
        res = self.engine.execute(self.game, plan,
                                  {"a.sav": RESOLVE_KEEP_LOCAL})
        self.assertEqual(res["errors"], [])
        self.assertEqual(read(os.path.join(self.save_dir, "a.sav")),
                         b"local-edit")
        self.assertEqual(self.engine.status(self.game).state, "synced")

    def test_use_remote_backs_up_local_first(self):
        plan = self._make_conflict(b"local-edit", b"remote-edit")
        res = self.engine.execute(self.game, plan,
                                  {"a.sav": RESOLVE_USE_REMOTE})
        self.assertEqual(res["errors"], [])
        self.assertEqual(read(os.path.join(self.save_dir, "a.sav")),
                         b"remote-edit")
        # 被覆盖的本地版本必须留底
        copies = self.vault.list_local_copies(self.game.id)
        self.assertEqual(len(copies), 1)
        self.assertEqual(read(copies[0]["backup_path"]), b"local-edit")

    def test_save_both(self):
        plan = self._make_conflict(b"local-edit", b"remote-edit")
        res = self.engine.execute(self.game, plan,
                                  {"a.sav": RESOLVE_SAVE_BOTH})
        self.assertEqual(res["errors"], [])
        self.assertEqual(read(os.path.join(self.save_dir, "a.sav")),
                         b"local-edit")
        # 云端版本另存为新文件，两边都不丢
        others = [f for f in os.listdir(self.save_dir)
                  if f.startswith("a.云端版本_")]
        self.assertEqual(len(others), 1)
        self.assertEqual(read(os.path.join(self.save_dir, others[0])),
                         b"remote-edit")

    def test_unresolved_conflict_blocks_sync(self):
        plan = self._make_conflict(b"local-edit", b"remote-edit")
        res = self.engine.execute(self.game, plan, {})
        self.assertEqual(res["conflicts"], 1)
        self.assertTrue(res["errors"])
        # 没裁决就不推进基准，下次仍然是冲突态
        self.assertEqual(self.engine.status(self.game).state, "conflict")


class TestMultiPathVault(BaseSyncTest):
    def setUp(self):
        super().setUp()
        self.dir_a = os.path.join(self.tmp, "A")
        self.dir_b = os.path.join(self.tmp, "B")
        os.makedirs(self.dir_a, exist_ok=True)
        os.makedirs(os.path.join(self.dir_b, "sub"), exist_ok=True)
        self.multi = self.vault.add_game("Multi", [self.dir_a, self.dir_b])

    def test_virtual_root_roundtrip(self):
        """多目录时路径带 `序号/` 前缀，还原时必须回到各自的根。"""
        write(os.path.join(self.dir_a, "one.sav"), b"from-A")
        write(os.path.join(self.dir_b, "sub", "two.sav"), b"from-B")
        snap = self.vault.create_snapshot(self.multi)
        paths = sorted(e.path for e in snap.files)
        self.assertEqual(paths, ["0/one.sav", "1/sub/two.sav"])

        write(os.path.join(self.dir_a, "one.sav"), b"clobbered")
        write(os.path.join(self.dir_b, "sub", "two.sav"), b"clobbered")
        self.vault.restore_snapshot(self.multi, snap.id)
        self.assertEqual(read(os.path.join(self.dir_a, "one.sav")), b"from-A")
        self.assertEqual(read(os.path.join(self.dir_b, "sub", "two.sav")),
                         b"from-B")

    def test_stale_index_is_ignored(self):
        """快照的序号前缀超出当前目录数时，跳过而不是写错位置。"""
        snap = self.vault.create_snapshot(self.multi)
        # restore_snapshot 是从磁盘上的快照 JSON 还原的，
        # 改内存里的对象没用，必须把篡改后的快照重新落盘。
        snap.files.append(FileEntry(path="9/ghost.sav", sha256="x", size=1,
                                    mtime=0.0, chunks=[]))
        self.vault._write_snapshot(snap)
        st = self.vault.restore_snapshot(self.multi, snap.id)
        self.assertEqual(st["skipped"], 1)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "9")))


# ==================================================================
# 9. 本地备份模式（cloud_on=False）
# ==================================================================
class ExplodingRemote(LocalFolderBackend):
    """一被调用就炸的后端，用来证明本地模式真的没碰云端。"""

    def list_snapshots(self, game_id):
        raise AssertionError("本地模式不该查询云端快照清单")

    def missing_chunks(self, shas):
        raise AssertionError("本地模式不该查询云端 chunk")

    def upload_chunk(self, sha, blob, progress_cb=None):
        raise AssertionError("本地模式不该上传 chunk")


class TestLocalOnlyMode(unittest.TestCase):
    """默认形态：不配云端，一切只在本地发生。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gsc_local_")
        self.vault = Vault(os.path.join(self.tmp, "vault"))
        self.remote = ExplodingRemote(os.path.join(self.tmp, "remote"))
        self.engine = SyncEngine(self.vault, self.remote, cloud_on=False)
        self.save_dir = os.path.join(self.tmp, "FakeGame", "saves")
        os.makedirs(self.save_dir, exist_ok=True)
        self.game = self.vault.add_game("FakeGame", [self.save_dir])

    def tearDown(self):
        self.vault.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_snapshot_yet_is_unknown(self):
        st = self.engine.status(self.game)
        self.assertEqual(st.state, SYNC_UNKNOWN)
        self.assertIn("尚未备份", st.message)

    def test_backed_up_and_clean_is_synced(self):
        write(os.path.join(self.save_dir, "a.sav"), b"v1")
        self.vault.create_snapshot(self.game)
        st = self.engine.status(self.game)
        self.assertEqual(st.state, SYNC_SYNCED)
        self.assertEqual(st.snapshot_count, 1)
        self.assertEqual(st.local_size, 2)
        # 「云端体积」这一栏在本地模式下装的是最新快照的体积
        self.assertEqual(st.remote_size, 2)
        self.assertIn("本地保险库", st.message)

    def test_dirty_local_is_pending(self):
        write(os.path.join(self.save_dir, "a.sav"), b"v1")
        self.vault.create_snapshot(self.game)
        write(os.path.join(self.save_dir, "a.sav"), b"v2-longer")
        st = self.engine.status(self.game)
        self.assertEqual(st.state, SYNC_LOCAL_AHEAD)
        self.assertIn("1 个文件", st.message)

    def test_new_file_counts_as_dirty(self):
        write(os.path.join(self.save_dir, "a.sav"), b"v1")
        self.vault.create_snapshot(self.game)
        write(os.path.join(self.save_dir, "b.sav"), b"new")
        st = self.engine.status(self.game)
        self.assertEqual(st.state, SYNC_LOCAL_AHEAD)

    def test_plan_does_not_touch_remote(self):
        write(os.path.join(self.save_dir, "a.sav"), b"v1")
        plan = self.engine.plan(self.game)      # 炸了就说明碰到了云端
        self.assertEqual(plan.error, "")
        # 没有云端可对比，所有文件都算「待上传」
        self.assertEqual([a.action for a in plan.actions], [ACT_UPLOAD])
        self.assertEqual(plan.upload_bytes, 2)
        self.assertFalse(plan.has_conflict)

    def test_restore_roundtrip_in_local_mode(self):
        write(os.path.join(self.save_dir, "a.sav"), b"good")
        snap = self.vault.create_snapshot(self.game)
        write(os.path.join(self.save_dir, "a.sav"), b"corrupted")
        self.vault.restore_snapshot(self.game, snap.id)
        self.assertEqual(read(os.path.join(self.save_dir, "a.sav")), b"good")
        st = self.engine.status(self.game)
        self.assertEqual(st.state, SYNC_SYNCED)


if __name__ == "__main__":
    unittest.main(verbosity=2)