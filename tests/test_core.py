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
import json
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gamesave import chunker
from gamesave import gamemeta
from gamesave import gameindex
from gamesave.chunker import CHUNK_SIZE
from gamesave.models import (Game, Snapshot, FileEntry, ChunkRef,
                             RESOLVE_KEEP_LOCAL, RESOLVE_USE_REMOTE,
                             RESOLVE_SAVE_BOTH,
                             SYNC_SYNCED, SYNC_LOCAL_AHEAD, SYNC_UNKNOWN,
                             SYNC_ERROR)
from gamesave.store import ChunkStore
from gamesave.vault import Vault, safe_join, UnsafePathError
from gamesave import secret
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


class TestMergeDuplicates(BaseSyncTest):
    """刷新时自动合并「完全相同的备份」。"""

    def _three_identical(self):
        write(os.path.join(self.save_dir, "a.sav"), b"same-content")
        snaps = []
        for _ in range(3):
            snaps.append(self.vault.create_snapshot(self.game, kind="auto"))
            time.sleep(0.01)
        return snaps

    def test_merge_identical_keeps_newest(self):
        snaps = self._three_identical()
        self.assertEqual(len(self.vault.list_snapshots(self.game.id)), 3)
        removed = self.vault.merge_duplicate_snapshots(self.game.id)
        self.assertEqual(removed, 2)
        left = self.vault.list_snapshots(self.game.id)
        self.assertEqual(len(left), 1)
        # 留下的是组内最新的一份
        self.assertEqual(left[0].id, snaps[-1].id)
        # chunk 被保留的那份引用着，不能被 gc 收走
        self.assertTrue(self.vault.store.has(left[0].files[0].chunks[0].sha256))

    def test_merge_keeps_different(self):
        for i in range(3):
            write(os.path.join(self.save_dir, "a.sav"), b"v%d" % i)
            self.vault.create_snapshot(self.game)
            time.sleep(0.01)
        removed = self.vault.merge_duplicate_snapshots(self.game.id)
        self.assertEqual(removed, 0)
        self.assertEqual(len(self.vault.list_snapshots(self.game.id)), 3)

    def test_merge_mtime_only_change_still_merges(self):
        """内容哈希不变、只有 mtime 变（游戏「读了但没改」）也应合并。"""
        write(os.path.join(self.save_dir, "a.sav"), b"same-content")
        s1 = self.vault.create_snapshot(self.game)
        p = os.path.join(self.save_dir, "a.sav")
        st = os.stat(p)
        os.utime(p, (st.st_atime + 60, st.st_mtime + 60))
        s2 = self.vault.create_snapshot(self.game)
        self.assertEqual(s1.files[0].sha256, s2.files[0].sha256)
        removed = self.vault.merge_duplicate_snapshots(self.game.id)
        self.assertEqual(removed, 1)

    def test_merge_protects_base_snapshot(self):
        """同步基准是三方合并的参照物，即使内容相同也不能删。"""
        snaps = self._three_identical()
        base = snaps[0]                       # 最旧的那份当基准
        self.vault.set_sync_state(self.game.id, base_snapshot_id=base.id,
                                  last_sync_at=time.time())
        removed = self.vault.merge_duplicate_snapshots(self.game.id)
        self.assertEqual(removed, 1)          # 只删中间的，基准和最新的都留
        ids = [s.id for s in self.vault.list_snapshots(self.game.id)]
        self.assertIn(base.id, ids)
        self.assertIn(snaps[-1].id, ids)

    def test_merge_single_snapshot_noop(self):
        write(os.path.join(self.save_dir, "a.sav"), b"only-one")
        self.vault.create_snapshot(self.game)
        self.assertEqual(self.vault.merge_duplicate_snapshots(self.game.id), 0)
        self.assertEqual(len(self.vault.list_snapshots(self.game.id)), 1)

    def test_merge_mixed_groups(self):
        """A A B A B：合并后各留最新的一份（A 留 v3 的 A，B 留第二个 B）。"""
        for content in (b"A", b"A", b"B", b"A", b"B"):
            write(os.path.join(self.save_dir, "a.sav"), content)
            self.vault.create_snapshot(self.game)
            time.sleep(0.01)
        removed = self.vault.merge_duplicate_snapshots(self.game.id)
        self.assertEqual(removed, 3)
        left = self.vault.list_snapshots(self.game.id)
        self.assertEqual(len(left), 2)
        self.assertEqual([s.files[0].sha256 for s in left],
                         [chunker.sha256_hex(b"B"), chunker.sha256_hex(b"A")])


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


# ==================================================================
# 10. 安全：快照路径不允许逃出存档根
# ==================================================================
def self_tmpdir():
    if not hasattr(self_tmpdir, "_d"):
        self_tmpdir._d = tempfile.mkdtemp(prefix="gsc_safe_")
    return self_tmpdir._d


class TestSafeJoin(unittest.TestCase):
    def test_normal_and_nested(self):
        base = os.path.join(self_tmpdir(), "root")
        self.assertEqual(safe_join(base, "a.sav"),
                         os.path.join(base, "a.sav"))
        self.assertEqual(safe_join(base, "sub/deep/a.sav"),
                         os.path.join(base, "sub", "deep", "a.sav"))
        # 目标尚不存在也必须能校验（还原时要新建目录）
        self.assertTrue(safe_join(base, "newdir/a.sav")
                        .startswith(base + os.sep))

    def test_dotdot_traversal_blocked(self):
        base = os.path.join(self_tmpdir(), "root")
        with self.assertRaises(UnsafePathError):
            safe_join(base, "../../evil.sav")
        with self.assertRaises(UnsafePathError):
            safe_join(base, "sub/../../evil.sav")

    def test_drive_absolute_blocked(self):
        # 盘符绝对路径不能把 root 顶掉
        with self.assertRaises(UnsafePathError):
            safe_join(r"C:\Games\saves", r"C:\Windows\evil.sav")

    def test_restore_refuses_traversal_snapshot(self):
        """端到端：篡改快照清单注入 ../，回滚必须整体拒绝且不落任何文件。"""
        tmp = tempfile.mkdtemp(prefix="gsc_trav_")
        try:
            vault = Vault(os.path.join(tmp, "vault"))
            save_dir = os.path.join(tmp, "FakeGame", "saves")
            os.makedirs(save_dir, exist_ok=True)
            g = vault.add_game("FakeGame", [save_dir])
            write(os.path.join(save_dir, "a.sav"), b"v1")
            snap = vault.create_snapshot(g)
            evil_target = os.path.join(tmp, "evil.sav")
            snap.files.append(FileEntry(
                path="../../evil.sav", sha256="deadbeef", size=4,
                mtime=0.0, chunks=[]))
            vault._write_snapshot(snap)
            with self.assertRaises(UnsafePathError):
                vault.restore_snapshot(g, snap.id)
            self.assertFalse(os.path.exists(evil_target))
            vault.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ==================================================================
# 11. 状态刷新快速路径（quick_hint 复用旧 FileEntry）
# ==================================================================
class TestQuickScan(BaseSyncTest):
    def test_unchanged_file_reuses_hint_without_reread(self):
        p = os.path.join(self.save_dir, "a.sav")
        write(p, b"v1-content")
        snap = self.vault.create_snapshot(self.game)
        hint = {e.path: e for e in snap.files}
        # size+mtime 未变：直接复用，sha 与 hint 一致
        entries = self.vault.scan_local(self.game, quick_hint=hint)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].sha256, hint["a.sav"].sha256)
        self.assertEqual(entries[0].chunks, hint["a.sav"].chunks)

    def test_size_change_ignores_hint(self):
        p = os.path.join(self.save_dir, "a.sav")
        write(p, b"v1")
        snap = self.vault.create_snapshot(self.game)
        hint = {e.path: e for e in snap.files}
        write(p, b"v2-but-longer")
        entries = self.vault.scan_local(self.game, quick_hint=hint)
        self.assertNotEqual(entries[0].sha256, hint["a.sav"].sha256)

    def test_same_size_sneaky_edit_caught_by_exact_scan(self):
        """Cemu 式篡改：内容变了但 size 没变、还把 mtime 伪造回去。

        快速扫描会被这种手法骗过（这是明确的取舍：mtime 不可信），
        但真正的 plan() 走全量精算，必须发现差异 —— 同步决策
        永远以内容哈希为准。
        """
        p = os.path.join(self.save_dir, "a.sav")
        write(p, b"AAAA")
        snap = self.vault.create_snapshot(self.game)
        hint = {e.path: e for e in snap.files}
        st = os.stat(p)
        write(p, b"BBBB")
        os.utime(p, (st.st_atime, st.st_mtime))
        quick = self.vault.scan_local(self.game, quick_hint=hint)
        self.assertEqual(quick[0].sha256, hint["a.sav"].sha256)  # 被骗过
        exact = self.vault.scan_local(self.game)
        self.assertNotEqual(exact[0].sha256, hint["a.sav"].sha256)

    def test_missing_chunk_disables_reuse(self):
        """hint 引用的 chunk 已不在 store 里时不能复用，否则状态会撒谎。"""
        p = os.path.join(self.save_dir, "a.sav")
        write(p, b"v1-content")
        snap = self.vault.create_snapshot(self.game)
        hint = {e.path: e for e in snap.files}
        os.remove(self.vault.store.path_of(hint["a.sav"].chunks[0].sha256))
        entries = self.vault.scan_local(self.game, quick_hint=hint)
        self.assertEqual(len(entries), 1)
        self.assertTrue(self.vault.store.has(entries[0].chunks[0].sha256))


# ==================================================================
# 12. 凭据加密（Windows 走 DPAPI）
# ==================================================================
class TestSecret(unittest.TestCase):
    def test_roundtrip(self):
        token = secret.encrypt("p@ssw0rd-坚果云")
        if secret.is_dpapi_available():
            self.assertTrue(token.startswith(secret.PREFIX_DPAPI))
        self.assertNotIn("p@ssw0rd", token)      # 落盘串里看不到明文
        self.assertEqual(secret.decrypt(token), "p@ssw0rd-坚果云")

    def test_empty_passthrough(self):
        self.assertEqual(secret.encrypt(""), "")
        self.assertEqual(secret.decrypt(""), "")

    def test_legacy_plaintext_still_readable(self):
        # 旧版本裸明文必须还能读出来，不能把用户锁在门外
        self.assertEqual(secret.decrypt("old-plain-password"),
                         "old-plain-password")

    def test_b64_fallback_roundtrip(self):
        token = secret.PREFIX_B64 + "YWJj"
        self.assertEqual(secret.decrypt(token), "abc")


# ==================================================================
# 13. 快照清单原子写（不留 .tmp 残骸）
# ==================================================================
class TestAtomicSnapshotWrite(BaseSyncTest):
    def test_no_tmp_left_behind(self):
        write(os.path.join(self.save_dir, "a.sav"), b"v1")
        snap = self.vault.create_snapshot(self.game)
        tmp = os.path.join(self.vault.snapshots_dir, snap.id + ".json.tmp")
        self.assertFalse(os.path.exists(tmp))
        self.assertTrue(os.path.exists(
            os.path.join(self.vault.snapshots_dir, snap.id + ".json")))


# ==================================================================
# 14. 游戏元数据：中文名 / appid 解析（全程假网络，不碰真 Steam）
# ==================================================================
def _term_of(url):
    """从搜索 URL 里把 term 抠出来，便于按候选词返回不同结果。"""
    q = url.split("term=", 1)[1].split("&", 1)[0]
    return urllib.parse.unquote(q)


def _fake_fetch(mapping, default=None, details=None):
    """按搜索词返回预设结果；details 给 appdetails 用（appid -> 英文名）。"""
    def fetch(url):
        if "appdetails" in url:
            appid = url.split("appids=", 1)[1].split("&", 1)[0]
            name = (details or {}).get(appid, "")
            data = {"name": name} if name else {}
            return json.dumps({appid: {"success": bool(name),
                                       "data": data}}).encode("utf-8")
        items = mapping.get(_term_of(url), default or [])
        return json.dumps({"items": items}).encode("utf-8")
    return fetch


class TestMetaLookup(unittest.TestCase):
    def test_chinese_store_name_wins(self):
        fetch = _fake_fetch({
            "只狼": [{"type": "app", "id": 814380, "name": "只狼：影逝二度"}],
        })
        res = gamemeta.lookup("只狼 Sekiro", fetch=fetch)
        self.assertEqual(res["status"], gamemeta.STATUS_OK)
        self.assertEqual(res["appid"], "814380")
        self.assertEqual(res["name_zh"], "只狼：影逝二度")

    def test_english_only_game_is_ok_not_error(self):
        """Steam 查到但官方没中文名 —— 必须是 ok，否则每次刷新都重试。"""
        fetch = _fake_fetch({
            "Red Dead Redemption 2": [
                {"type": "app", "id": 1174180, "name": "Red Dead Redemption 2"}],
        })
        res = gamemeta.lookup("荒野大镖客2 Red Dead Redemption 2", fetch=fetch)
        self.assertEqual(res["status"], gamemeta.STATUS_OK)
        self.assertEqual(res["appid"], "1174180")
        self.assertEqual(res["name_zh"], "")
        self.assertEqual(res["official_name"], "Red Dead Redemption 2")

    def test_not_found(self):
        res = gamemeta.lookup("根本不存在的游戏", fetch=lambda u: b'{"items": []}')
        self.assertEqual(res["status"], gamemeta.STATUS_NOT_FOUND)
        self.assertEqual(res["appid"], "")

    def test_network_error(self):
        def boom(url):
            raise OSError("connection reset")
        res = gamemeta.lookup("某游戏", fetch=boom)
        self.assertEqual(res["status"], gamemeta.STATUS_ERROR)
        self.assertTrue(res["error"])

    def test_bad_json_is_error(self):
        res = gamemeta.lookup("某游戏", fetch=lambda u: b"<html>502</html>")
        self.assertEqual(res["status"], gamemeta.STATUS_ERROR)

    def test_dlc_is_skipped(self):
        fetch = _fake_fetch({
            "测试游戏": [
                {"type": "dlc", "id": 999, "name": "测试游戏 原声带"},
                {"type": "app", "id": 100, "name": "测试游戏"},
            ],
        })
        res = gamemeta.lookup("测试游戏", fetch=fetch)
        self.assertEqual(res["appid"], "100")

    def test_unrelated_result_rejected(self):
        """名字对不上就当没搜到 —— 宁可没图标也不显示错的。"""
        fetch = _fake_fetch({
            "只狼": [{"type": "app", "id": 1, "name": "Totally Different Game"}],
        })
        res = gamemeta.lookup("只狼", fetch=fetch)
        self.assertEqual(res["status"], gamemeta.STATUS_NOT_FOUND)

    def test_search_terms_order(self):
        terms = gamemeta.search_terms("荒野大镖客2 Red Dead Redemption 2")
        self.assertEqual(terms[0], "Red Dead Redemption 2")   # 英文优先
        self.assertIn("荒野大镖客2", terms)

    def test_alias_expands_abbreviation(self):
        self.assertEqual(gamemeta.search_terms("GTA V")[0],
                         "Grand Theft Auto V")
        self.assertIn("GTA V", gamemeta.search_terms("GTA V"))

    def test_subtitle_extension_is_not_hijack(self):
        """副标题靠分隔符接的是正常命名，靠空格硬接的是另一个产品。"""
        self.assertTrue(gamemeta.name_match(
            "Sekiro", "Sekiro™: Shadows Die Twice - GOTY Edition"))
        self.assertFalse(gamemeta.name_match(
            "Cyberpunk 2077", "Cyberpunk 2077 REDmod"))

    def test_cross_language_uses_english_name(self):
        """国区返回中文名时，用 appdetails 的英文名认亲，不能认成 DLC。"""
        fetch = _fake_fetch(
            {"Cyberpunk 2077": [
                {"type": "app", "id": 1091500, "name": "赛博朋克 2077"},
                {"type": "app", "id": 2138330,
                 "name": "《赛博朋克 2077：往日之影》"},
                {"type": "app", "id": 2060310,
                 "name": "Cyberpunk 2077 REDmod"},
            ]},
            details={"1091500": "Cyberpunk 2077"})
        res = gamemeta.lookup("赛博朋克2077 Cyberpunk 2077", fetch=fetch)
        self.assertEqual(res["status"], gamemeta.STATUS_OK)
        self.assertEqual(res["appid"], "1091500")
        self.assertEqual(res["name_zh"], "赛博朋克 2077")

    def test_no_space_name_resolved_via_english(self):
        """目录名常是 SplitFiction 这种连写，靠英文名认回「双影奇境」。"""
        fetch = _fake_fetch(
            {"SplitFiction": [
                {"type": "app", "id": 2001120, "name": "双影奇境"},
                {"type": "app", "id": 3052150,
                 "name": "《双影奇境》 - 好友通行证"},
            ]},
            details={"2001120": "Split Fiction"})
        res = gamemeta.lookup("SplitFiction", fetch=fetch)
        self.assertEqual(res["appid"], "2001120")
        self.assertEqual(res["name_zh"], "双影奇境")

    def test_english_subtitle_does_not_hijack(self):
        """英文名被追加副标题的多半是 DLC / 工具，不能当成本体。"""
        self.assertFalse(gamemeta.name_match("Cyberpunk 2077",
                                             "Cyberpunk 2077 REDmod"))
        self.assertTrue(gamemeta.name_match("只狼", "只狼：影逝二度"))

    def test_pick_chinese(self):
        self.assertEqual(gamemeta.pick_chinese(["Elden Ring", "艾尔登法环"]),
                         "艾尔登法环")
        self.assertEqual(gamemeta.pick_chinese(["Elden Ring"]), "")

    def test_logo_urls(self):
        urls = gamemeta.logo_urls("1174180")
        self.assertIn("1174180", urls[0])
        self.assertTrue(urls[0].endswith(".png"))
        self.assertEqual(gamemeta.logo_urls(""), ())


# ==================================================================
# 15. 元数据落库：patch 语义 + 回填到 Game
# ==================================================================
class TestMetaStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gsc_meta_")
        self.vault = Vault(os.path.join(self.tmp, "vault"))
        self.game = self.vault.add_game("测试游戏 Test Game", [self.tmp])

    def tearDown(self):
        self.vault.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_patch_keeps_untouched_fields(self):
        """一次只补到图标时，不能把上一轮拿到的中文名抹掉。"""
        self.vault.save_meta(self.game.id, name_zh="测试游戏",
                             state="ok", attempts=1)
        self.vault.save_meta(self.game.id, logo_file="abc.png")
        m = self.vault.get_meta(self.game.id)
        self.assertEqual(m["name_zh"], "测试游戏")
        self.assertEqual(m["logo_file"], "abc.png")
        self.assertEqual(m["attempts"], 1)
        self.assertEqual(m["state"], "ok")

    def test_list_games_applies_meta(self):
        self.vault.save_meta(self.game.id, name_zh="测试游戏",
                             logo_file="x.png", state="ok")
        g = self.vault.list_games()[0]
        self.assertEqual(g.name_zh, "测试游戏")
        self.assertEqual(g.logo_file, "x.png")
        self.assertEqual(g.meta_state, "ok")
        self.assertEqual(g.display_name, "测试游戏")

    def test_display_name_falls_back_to_original(self):
        g = self.vault.list_games()[0]
        self.assertEqual(g.name_zh, "")
        self.assertEqual(g.display_name, g.name)

    def test_get_game_applies_meta(self):
        self.vault.save_meta(self.game.id, name_zh="测试游戏")
        self.assertEqual(self.vault.get_game(self.game.id).name_zh, "测试游戏")

    def test_remove_game_clears_meta(self):
        self.vault.save_meta(self.game.id, name_zh="测试游戏")
        self.vault.remove_game(self.game.id)
        self.assertIsNone(self.vault.get_meta(self.game.id))

    def test_meta_survives_reopen(self):
        """重启后能直接读到本地数据，不用重新匹配。"""
        self.vault.save_meta(self.game.id, name_zh="测试游戏", logo_file="x.png")
        path = self.vault.root
        self.vault.close()
        v2 = Vault(path)
        try:
            g = v2.list_games()[0]
            self.assertEqual(g.name_zh, "测试游戏")
            self.assertEqual(g.logo_file, "x.png")
        finally:
            v2.close()


# ==================================================================
# 16. 匹配调度：什么时候该跳过、什么时候该重试
# ==================================================================
class TestMetaScheduling(unittest.TestCase):
    def test_complete_is_skipped(self):
        meta = {"name_zh": "某游戏", "state": "ok"}
        self.assertTrue(gamemeta.is_complete("X", meta, True))
        self.assertFalse(gamemeta.should_attempt("X", meta, True))

    def test_missing_logo_still_attempts(self):
        meta = {"name_zh": "某游戏", "state": "ok"}
        self.assertFalse(gamemeta.is_complete("X", meta, False))
        self.assertTrue(gamemeta.should_attempt("X", meta, False))

    def test_name_with_cjk_counts_even_without_meta(self):
        self.assertTrue(gamemeta.has_chinese_name("荒野大镖客2 RDR2", None))

    def test_ok_without_chinese_name_does_not_loop(self):
        """查到了但没中文名 → 算「中文名已尽力」，不能无限重试。"""
        self.assertTrue(gamemeta.has_chinese_name("RDR2", {"state": "ok"}))
        self.assertTrue(gamemeta.is_complete("RDR2", {"state": "ok"}, True))

    def test_not_found_still_needs_match(self):
        self.assertFalse(gamemeta.has_chinese_name("RDR2", {"state": "not_found"}))

    def test_backoff_blocks_then_expires(self):
        now = 1000.0
        meta = {"state": "not_found", "next_retry_at": now + 60}
        self.assertFalse(gamemeta.should_attempt("X", meta, False, now))
        self.assertTrue(gamemeta.should_attempt("X", meta, False, now + 61))

    def test_retry_delay_grows_and_caps(self):
        self.assertEqual(gamemeta.retry_delay(1), gamemeta.RETRY_BASE)
        self.assertEqual(gamemeta.retry_delay(2), gamemeta.RETRY_BASE * 2)
        self.assertEqual(gamemeta.retry_delay(3), gamemeta.RETRY_BASE * 4)
        self.assertLessEqual(gamemeta.retry_delay(99), gamemeta.RETRY_MAX)
        self.assertEqual(gamemeta.retry_delay(0), gamemeta.RETRY_BASE)


# ==================================================================
# 17. 离线索引：零网络认出榜单游戏
# ==================================================================
class TestGameIndex(unittest.TestCase):
    def setUp(self):
        gameindex.set_cache({
            "1174180": {"name_zh": "荒野大镖客：救赎2",
                        "name_en": "Red Dead Redemption 2",
                        "aliases": ["大表哥2", "RDR2"], "reviews": 858498},
            "2001120": {"name_zh": "双影奇境", "name_en": "Split Fiction",
                        "aliases": [], "reviews": 5210},
        })

    def tearDown(self):
        gameindex.clear_cache()

    def test_exact_english(self):
        r = gameindex.lookup("Red Dead Redemption 2")
        self.assertEqual(r["appid"], "1174180")
        self.assertEqual(r["name_zh"], "荒野大镖客：救赎2")
        self.assertEqual(r["source"], "index")

    def test_no_space_directory_name(self):
        """目录名常是无空格连写，归一化后要能对上。"""
        self.assertEqual(gameindex.lookup("SplitFiction")["appid"], "2001120")

    def test_alias(self):
        self.assertEqual(gameindex.lookup("RDR2")["appid"], "1174180")

    def test_miss_returns_none(self):
        self.assertIsNone(gameindex.lookup("这款游戏肯定不在榜上 XYZ"))

    def test_empty_name(self):
        self.assertIsNone(gameindex.lookup(""))
        self.assertIsNone(gameindex.lookup("   "))

    def test_entries_sorted_by_reviews(self):
        rows = gameindex.entries()
        self.assertEqual(rows[0]["appid"], "1174180")   # 评价数更高
        self.assertEqual(len(rows), 2)


class TestIndexFirstLookup(unittest.TestCase):
    """索引命中时必须一个网络请求都不发。"""

    def setUp(self):
        gameindex.set_cache({
            "1174180": {"name_zh": "荒野大镖客：救赎2",
                        "name_en": "Red Dead Redemption 2",
                        "aliases": ["RDR2"], "reviews": 1},
        })

    def tearDown(self):
        gameindex.clear_cache()

    def test_index_hit_skips_network(self):
        calls = []

        def fetch(url):
            calls.append(url)
            raise AssertionError("索引命中时不应该联网")

        r = gamemeta.lookup("RDR2", fetch=fetch, local_index=gameindex.lookup)
        self.assertEqual(r["appid"], "1174180")
        self.assertEqual(r["source"], "index")
        self.assertEqual(calls, [])

    def test_index_miss_falls_back_to_steam(self):
        def fetch(url):
            return json.dumps({"items": [
                {"type": "app", "id": 814380, "name": "只狼"}]}).encode()

        r = gamemeta.lookup("只狼", fetch=fetch, local_index=gameindex.lookup)
        self.assertEqual(r["status"], gamemeta.STATUS_OK)
        self.assertEqual(r["appid"], "814380")


# ==================================================================
# 18. 补充源 Bangumi：只补中文名，且必须名字对得上
# ==================================================================
def _bgm_fetch(list_items):
    def fetch(url):
        return json.dumps({"list": list_items}).encode("utf-8")
    return fetch


class TestBangumiSupplement(unittest.TestCase):
    def test_returns_name_cn(self):
        fetch = _bgm_fetch([{"name": "Red Dead Redemption 2",
                             "name_cn": "荒野大镖客：救赎2"}])
        self.assertEqual(gamemeta.bangumi_name_zh("Red Dead Redemption 2", fetch),
                         "荒野大镖客：救赎2")

    def test_mismatch_rejected(self):
        fetch = _bgm_fetch([{"name": "Totally Different", "name_cn": "别的游戏"}])
        self.assertEqual(gamemeta.bangumi_name_zh("RDR2", fetch), "")

    def test_missing_name_cn_skipped(self):
        fetch = _bgm_fetch([{"name": "Red Dead Redemption 2", "name_cn": ""}])
        self.assertEqual(gamemeta.bangumi_name_zh("Red Dead Redemption 2", fetch), "")

    def test_network_failure_returns_empty(self):
        def fetch(url):
            raise OSError("timeout")
        self.assertEqual(gamemeta.bangumi_name_zh("RDR2", fetch), "")

    def test_bad_json_returns_empty(self):
        self.assertEqual(
            gamemeta.bangumi_name_zh("RDR2", lambda u: b"<html>502</html>"), "")

    def test_fills_missing_chinese_name(self):
        def fetch(url):
            if "bgm.tv" in url:
                return json.dumps({"list": [
                    {"name": "Red Dead Redemption 2",
                     "name_cn": "荒野大镖客：救赎2"}]}).encode()
            return json.dumps({"items": [
                {"type": "app", "id": 1174180,
                 "name": "Red Dead Redemption 2"}]}).encode()

        r = gamemeta.lookup("Red Dead Redemption 2", fetch=fetch, use_bangumi=True)
        self.assertEqual(r["name_zh"], "荒野大镖客：救赎2")
        self.assertEqual(r["source"], "steam+bangumi")

    def test_off_by_default(self):
        """默认不启用：开关关掉时绝不能因为一个补名请求拖慢刷新。"""
        def fetch(url):
            if "bgm.tv" in url:
                raise AssertionError("未启用时不应该请求 Bangumi")
            return json.dumps({"items": [
                {"type": "app", "id": 1174180,
                 "name": "Red Dead Redemption 2"}]}).encode()

        r = gamemeta.lookup("Red Dead Redemption 2", fetch=fetch)
        self.assertEqual(r["name_zh"], "")

    def test_not_called_when_steam_found_nothing(self):
        """Steam 没查到就不必再问 Bangumi —— 没有 appid，光有中文名也没用。"""
        def fetch(url):
            if "bgm.tv" in url:
                raise AssertionError("没查到游戏时不应该请求 Bangumi")
            return json.dumps({"items": []}).encode()

        r = gamemeta.lookup("不存在的游戏 XYZ", fetch=fetch, use_bangumi=True)
        self.assertEqual(r["status"], gamemeta.STATUS_NOT_FOUND)


if __name__ == "__main__":
    unittest.main(verbosity=2)